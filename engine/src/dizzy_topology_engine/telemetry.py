"""The topology engine's spans, added to the Simple Engine's trace (spec §9).

One trace per engine run, rooted at ``dizzy.run``, with the Simple Engine's span names,
derived ids, JSON-lines sink and OTLP export: an inline unit is traced exactly as the
Simple Engine traces it. What lanes add:

- every ``dizzy.unit`` carries ``dizzy.lane``, ``dizzy.executor``, ``dizzy.priority``,
  ``dizzy.queued_ms`` (enqueue to claim) and, when it re-ran an interrupted attempt,
  ``dizzy.recovered``. The run carries the placement (``dizzy.lanes``);
- a POOLED unit spans claim to settle, as an inline one does, and holds, on the worker's
  own clock, ``dizzy.pool.wait`` (queued in the pool), ``dizzy.procedure <name>`` (run
  in the worker, ``process.pid``) and ``dizzy.pool.handoff`` (finished, waiting for the
  host thread to collect it), then the usual ``dizzy.append``, projections and policies;
- ``dizzy.pool <lane>`` spans a pool's life (built on the lane's first claim, shut down
  with the engine or when a worker dies), and under it ``dizzy.worker`` each worker's
  start: the ``pool_domains`` it built.

And two up/down counters per lane, store and priority (:class:`Metrics`):
``dizzy.lane.queued`` (commands waiting for a claim, interrupted attempts included) and
``dizzy.lane.in_flight`` (claimed, not yet settled). Each change is one data point at the
moment it happened, so the series is exact rather than sampled. They go to
``<data>/telemetry/metrics/<run>.jsonl`` and, as OTLP metrics, to the collector.

Never the source of truth: spans link to the log by ids, and losing them changes no
decision. This module is the one place the engine reaches past the tracer's ``span``:
a span whose start is earlier than the code that ends it (a pooled unit, a pool), or
whose times come from another process (a worker's), is written here, through the
tracer's own sinks and in its record format.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dizzy_engine.trace import BATCH, ENGINE, Otlp, Tracer, otlp_span, otlp_value


@dataclass(frozen=True)
class Clock:
    """A moment on the host: wall clock (comparable with a worker's) and monotonic."""

    ns: int
    perf: float

    @classmethod
    def now(cls) -> Clock:
        return cls(time.time_ns(), time.perf_counter())


class Telemetry:
    """The engine's tracer, wrapped. Behaves as the Simple Engine's tracer everywhere the
    inherited engine uses it, and adds the lane attributes to each ``dizzy.unit`` it
    opens (set by the topology engine just before it runs an inline unit)."""

    def __init__(self, tracer: Any):
        self.inner = tracer
        self.on = isinstance(tracer, Tracer)
        self.unit_attrs: dict[str, Any] = {}
        self.metrics = Metrics(tracer)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def flush(self) -> None:
        self.inner.flush()
        self.metrics.flush()

    def close(self) -> None:
        self.inner.close()
        self.metrics.close()

    @contextmanager
    def span(
        self, name: str, span_id: str, parent: str | None = None, /, **attrs: Any
    ) -> Iterator[dict[str, Any]]:
        if name == "dizzy.unit":
            attrs = {**attrs, **self.unit_attrs}
        with self.inner.span(name, span_id, parent, **attrs) as live:
            yield live

    def annotate_run(self, **attrs: Any) -> None:
        """Attributes on ``dizzy.run``, which the tracer writes at close."""
        if self.on:
            self.inner.run[-1].update(attrs)

    def record(
        self, name: str, span_id: str, parent: str | None, start_ns: int, end_ns: int, **attrs: Any
    ) -> None:
        """A finished span with explicit wall-clock times (a worker's)."""
        if not self.on:
            return
        attrs.setdefault("dizzy.outcome", "ok")
        tracer = self.inner
        if tracer.file is not None:
            record = {
                "name": name,
                "trace_id": tracer.trace_id,
                "span_id": span_id,
                "parent_id": parent,
                "start": datetime.fromtimestamp(start_ns / 1e9, UTC).isoformat(),
                "duration_ms": round(max(end_ns - start_ns, 0) / 1e6, 3),
                "attributes": attrs,
            }
            tracer.file.write(json.dumps(record) + "\n")
        if tracer.otlp is not None:
            span = otlp_span(name, tracer.trace_id, span_id, parent, start_ns, end_ns, attrs)
            tracer.otlp.add(span)

    @contextmanager
    def span_since(
        self, name: str, span_id: str, parent: str | None, start: Clock, /, **attrs: Any
    ) -> Iterator[dict[str, Any]]:
        """A span that began at *start* (before this code ran) and ends when the block
        does; spans opened inside it are its children, as with the tracer's ``span``."""
        if not self.on:
            yield attrs
            return
        self.inner.stack.append(span_id)
        try:
            yield attrs
        except BaseException as exc:
            attrs.setdefault("dizzy.outcome", "error")
            attrs.setdefault("dizzy.error", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self.inner.stack.pop()
            end_ns = start.ns + int((time.perf_counter() - start.perf) * 1e9)
            self.record(name, span_id, parent or self.inner.stack[-1], start.ns, end_ns, **attrs)


# ── metrics ───────────────────────────────────────────────────────────────────

INSTRUMENTS = {
    "dizzy.lane.queued": ("{command}", "commands waiting for a claim on the lane"),
    "dizzy.lane.in_flight": ("{command}", "commands claimed on the lane and not yet settled"),
}


def metrics_endpoint() -> str | None:
    """The metrics URL from the standard OTLP variables, or None to export nothing."""
    url = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
    if url:
        return url
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    return base.rstrip("/") + "/v1/metrics" if base else None


class Metrics:
    """The engine's up/down counters (OpenTelemetry ``UpDownCounter``: a cumulative,
    non-monotonic sum), one series per instrument and attribute set. Every change is
    written as a data point at the moment it happened: to ``metrics/<run>.jsonl`` beside
    the run's spans, and over OTLP when the environment asks, batched like the spans.
    Off when the tracer is (no telemetry directory and no endpoint)."""

    def __init__(self, tracer: Any):
        self.on = isinstance(tracer, Tracer)
        self.start_ns = time.time_ns()
        self.values: dict[tuple[str, tuple[tuple[str, Any], ...]], int] = {}
        self.file = None
        self.otlp: OtlpMetrics | None = None
        if not self.on:
            return
        if tracer.file is not None:
            spans = Path(tracer.file.name)
            (spans.parent / "metrics").mkdir(exist_ok=True)
            self.file = open(spans.parent / "metrics" / spans.name, "a", buffering=1)  # noqa: SIM115
        url = metrics_endpoint()
        if url:
            self.otlp = OtlpMetrics(url, tracer.session, self.start_ns)

    def add(self, name: str, delta: int, **attrs: Any) -> None:
        if self.on and delta:
            key = (name, tuple(sorted(attrs.items())))
            self._point(key, self.values.get(key, 0) + delta)

    def reset(self, name: str, values: dict[tuple[tuple[str, Any], ...], int]) -> None:
        """Set *name*'s series to *values* (keyed by sorted attribute items); a series not
        in *values* goes to zero. A point is written for every series named."""
        if not self.on:
            return
        known = {attrs for (n, attrs) in self.values if n == name}
        for attrs in sorted(known | set(values)):
            if (name, attrs) in self.values or values.get(attrs):
                self._point((name, attrs), values.get(attrs, 0))

    def _point(self, key: tuple[str, tuple[tuple[str, Any], ...]], value: int) -> None:
        self.values[key] = value
        name, attrs = key
        at = time.time_ns()
        if self.file is not None:
            record = {
                "name": name,
                "time": datetime.fromtimestamp(at / 1e9, UTC).isoformat(),
                "value": value,
                "attributes": dict(attrs),
            }
            self.file.write(json.dumps(record) + "\n")
        if self.otlp is not None:
            self.otlp.add((name, dict(attrs), at, value))

    def flush(self) -> None:
        if self.otlp is not None:
            self.otlp.flush()

    def close(self) -> None:
        self.flush()
        if self.file is not None:
            self.file.close()
            self.file = None


class OtlpMetrics(Otlp):
    """The Simple Engine's OTLP batcher (a failed send drops the batch and pauses
    export), sending data points to ``/v1/metrics`` instead of spans."""

    def __init__(self, url: str, session: str, start_ns: int):
        super().__init__(url, session)
        self.start_ns = start_ns

    def add(self, point: Any) -> None:  # (name, attributes, time_ns, value)
        if time.monotonic() < self.paused_until:
            return
        self.spans.append(point)
        if len(self.spans) >= BATCH:
            self.flush()

    def payload(self, points: list[Any]) -> dict[str, Any]:
        by_domain: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for name, attrs, at, value in points:
            series = by_domain.setdefault(str(attrs.get("dizzy.domain", "")), {})
            series.setdefault(name, []).append(
                {
                    "attributes": [
                        {"key": k, "value": otlp_value(v)}
                        for k, v in {**attrs, "dizzy.session": self.session}.items()
                    ],
                    "startTimeUnixNano": str(self.start_ns),
                    "timeUnixNano": str(at),
                    "asInt": str(value),
                }
            )
        return {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": domain}},
                            {"key": "dizzy.engine", "value": {"stringValue": ENGINE}},
                            {"key": "dizzy.session", "value": {"stringValue": self.session}},
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": "dizzy-topology-engine"},
                            "metrics": [
                                {
                                    "name": name,
                                    "unit": INSTRUMENTS[name][0],
                                    "description": INSTRUMENTS[name][1],
                                    "sum": {
                                        "aggregationTemporality": 2,  # cumulative
                                        "isMonotonic": False,  # an UpDownCounter
                                        "dataPoints": data,
                                    },
                                }
                                for name, data in series.items()
                            ],
                        }
                    ],
                }
                for domain, series in by_domain.items()
            ]
        }
