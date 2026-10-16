"""Profiler — the stage profile of a captured run, from a JSONL bus capture.

The dataset is the file a ``JsonlBusSink`` wrote: one JSON object per line, each an
engine bus record stamped with ``seq`` and ``ts``. The questions answered here are the
ones a topology decision needs first — where the wall clock went per element, how wide
each policy fans out, and whether the workload is bound by work that must stay on the
fold lane or by work a pool could absorb.

Everything in this module is a pure function of (feat, parsed records) returning data
or a rendered string, so the tests are unit cases and one syrupy snapshot; the CLI verb
in ``cli.py`` owns file IO and error exits. Core-install deps only (stdlib plus the
feat loader): profiling must work without the ``gen`` extra, and a capture is read long
after the run, possibly on another machine.
"""

import json
import math
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime

from dizzy.feat_schema import FeatureDefinition
from dizzy.validation import pascal_case as _pascal

# The bucket split of the bottleneck statement. Projections and policies observe the
# stream and so never leave the fold lane; a procedure joins them the moment it declares
# a query (topology rules 3 and 6 — a reader shares a group with what it reads). Only a
# query-free procedure computes from its command alone and may move to a pool lane — or
# one whose every query reads a model the topology declares read-only, which never changes.
_SERIAL_KINDS = ("projection", "policy")
_ELEMENT_KINDS = ("procedure", "projection", "policy", "query")
_WORKER_COUNTS = (2, 4, 8)


def parse_records(lines: list[str]) -> tuple[list[dict], int]:
    """Parse capture lines into records, counting the lines that are not records.

    Telemetry is observation, never truth: a truncated final line (the process died
    mid-write) or stray shell output in the file must cost that line, not the profile.
    Returns ``(records, skipped)``; a record is any JSON object.
    """
    records: list[dict] = []
    skipped = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            skipped += 1
    return records, skipped


def command_owner(feat: FeatureDefinition) -> dict[str, str]:
    """Map each command CLASS name (PascalCase) to the feat name of its procedure.

    Pool procedure records are emitted parent-side by the ``LaneExecutor``, which only
    knows the command it shipped — so they carry the command class name (e.g.
    ``AnalyzeSample``) as both ``name`` and ``trigger``, while inline records carry the
    procedure's feat name. This map is what folds both spellings back onto one element,
    so a procedure that ran in both modes aggregates under one row.
    """
    return {_pascal(p.command): p.name for p in feat.procedures or []}


def procedure_queries(
    feat: FeatureDefinition, read_only: Collection[str] = ()
) -> dict[str, list[str]]:
    """Map each procedure's feat name to the queries it declares over a model that can
    change — its pool eligibility. Queries over *read_only* models are left out.

    A procedure with an empty list partitions freely; a non-empty list is the exact
    reason it cannot leave the fold lane, so the report prints the list, not a bare "no".
    """
    query_model = {q.name: q.model for q in feat.queries or []}
    return {
        p.name: [q for q in p.queries or [] if query_model.get(q) not in read_only]
        for p in feat.procedures or []
    }


@dataclass
class ElementStats:
    """Aggregated bus records for one (kind, name) element."""

    kind: str
    name: str
    count: int = 0
    errors: int = 0
    lanes: set[str] = field(default_factory=set)
    durations: list[float] = field(default_factory=list)
    input_items: int = 0
    result_items: int = 0
    access_patterns: Counter = field(default_factory=Counter)

    @property
    def total_ms(self) -> float:
        return sum(self.durations)


@dataclass
class Profile:
    """Everything the report renders, computed once from the parsed records."""

    first_ts: str | None
    last_ts: str | None
    record_count: int
    elements: dict[tuple[str, str], ElementStats]
    event_counts: Counter
    policy_counts: Counter


def aggregate(feat: FeatureDefinition, records: list[dict]) -> Profile:
    """Fold the capture into per-element stats, normalizing pool records via the feat.

    ``duration_ms`` may be ``None`` (event records) or absent; only real durations enter
    the distribution, but every record still counts an invocation. Error records keep
    their durations — the time was spent whether or not the element succeeded.
    """
    owner = command_owner(feat)
    elements: dict[tuple[str, str], ElementStats] = {}
    event_counts: Counter = Counter()
    policy_counts: Counter = Counter()
    stamps = [r["ts"] for r in records if isinstance(r.get("ts"), str)]

    for record in records:
        kind = record.get("kind")
        name = record.get("name")
        if not isinstance(name, str):
            continue
        if kind == "event":
            event_counts[name] += 1
            continue
        if kind not in _ELEMENT_KINDS:
            continue  # shell kinds (job, worker) are lifecycle, not stage work
        if kind == "procedure":
            name = owner.get(name, name)
        stats = elements.setdefault((kind, name), ElementStats(kind=kind, name=name))
        stats.count += 1
        if record.get("outcome") == "error":
            stats.errors += 1
        if isinstance(record.get("lane"), str):
            stats.lanes.add(record["lane"])
        if isinstance(record.get("duration_ms"), (int, float)):
            stats.durations.append(float(record["duration_ms"]))
        if kind == "query":
            if isinstance(record.get("input_items"), int):
                stats.input_items += max(0, record["input_items"])
            if isinstance(record.get("result_items"), int):
                stats.result_items += max(0, record["result_items"])
            input_shape = record.get("input_shape")
            result_shape = record.get("result_shape")
            if isinstance(input_shape, str) and isinstance(result_shape, str):
                stats.access_patterns[f"{input_shape}->{result_shape}"] += 1
        if kind == "policy":
            policy_counts[name] += 1

    return Profile(
        first_ts=min(stamps) if stamps else None,
        last_ts=max(stamps) if stamps else None,
        record_count=len(records),
        elements=elements,
        event_counts=event_counts,
        policy_counts=policy_counts,
    )


def bottleneck_totals(
    feat: FeatureDefinition, profile: Profile, read_only: Collection[str] = ()
) -> tuple[float, float]:
    """The two totals of the bottleneck statement: ``(serial_bound_ms, pool_eligible_ms)``.

    Serial-bound is every projection, every policy, and every procedure that declares
    queries over a model that can change — the work topology rules 3 and 6 pin to the fold
    lane. Pool-eligible is the rest of the procedures, including those that query only
    *read_only* models. A procedure the feat does not declare (a stale capture, or a
    capture from a different feature) is counted serial-bound: eligibility is a promise
    read from the feat, and an element the feat cannot vouch for gets none.
    """
    queries = procedure_queries(feat, read_only)
    serial = 0.0
    eligible = 0.0
    for (kind, name), stats in profile.elements.items():
        # Query calls are nested inside procedure/policy durations. They are
        # reported for attribution but are not additive topology work.
        if kind == "query":
            continue
        if kind in _SERIAL_KINDS:
            serial += stats.total_ms
        elif name in queries and not queries[name]:
            eligible += stats.total_ms
        else:
            serial += stats.total_ms
    return serial, eligible


def _percentile(durations: list[float], percentile: float) -> float:
    """Nearest-rank percentile, exact from a single observation upward."""
    ordered = sorted(durations)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


def _makespan_seconds(first_ts: str | None, last_ts: str | None) -> float | None:
    """Wall-clock span of the capture, or ``None`` when the stamps don't cooperate.

    ``TypeError`` is the naive-vs-aware subtraction (one hand-edited or foreign-writer
    stamp without an offset alongside the sink's ``+00:00`` ones) — like an unparseable
    stamp, it must cost the span line, not the profile.
    """
    if first_ts is None or last_ts is None:
        return None
    try:
        span = datetime.fromisoformat(last_ts) - datetime.fromisoformat(first_ts)
    except (ValueError, TypeError):
        return None
    return span.total_seconds()


def _fmt_ms(value: float) -> str:
    """Millisecond figures at one decimal — the resolution inline records carry."""
    return f"{value:.1f}"


def _table(headers: list[str], rows: list[list[str]], numeric_from: int) -> list[str]:
    """Render rows under headers with computed widths; columns >= *numeric_from* right-align."""
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row, strict=True)]

    def render(row: list[str]) -> str:
        cells = [
            cell.rjust(w) if i >= numeric_from else cell.ljust(w)
            for i, (cell, w) in enumerate(zip(row, widths, strict=True))
        ]
        return "  ".join(cells).rstrip()

    return [render(headers), render(["-" * w for w in widths])] + [render(r) for r in rows]


def render_profile(
    feat: FeatureDefinition, records: list[dict], read_only: Collection[str] = ()
) -> str:
    """Render the stage profile of *records* against *feat* as a plain-text report.

    The layout is the requirements doc's (``dizzy docs cli``, "dizzy profile"): capture
    span, the per-element table sorted by total time, fan-out counts, then the
    bottleneck statement — the one fact a topology decision needs first. *read_only* is
    the topology's read-only models: a procedure querying only those is pool-eligible.
    """
    profile = aggregate(feat, records)
    queries = procedure_queries(feat, read_only)
    declared = procedure_queries(feat)
    lines: list[str] = []

    # -- 1. Header -------------------------------------------------------------------
    makespan = _makespan_seconds(profile.first_ts, profile.last_ts)
    if makespan is None:
        lines.append("Capture span: unknown (no usable ts stamps)")
    else:
        lines.append(f"Capture span: {profile.first_ts} -> {profile.last_ts}")
        lines.append(f"Makespan:     {makespan:.2f}s")
    lines.append(
        f"Records:      {profile.record_count} "
        f"({sum(profile.event_counts.values())} events appended)"
    )
    lines.append("")

    # -- 2. Per-element table, heaviest stage first ------------------------------------
    ranked = sorted(profile.elements.values(), key=lambda s: (-s.total_ms, s.kind, s.name))
    rows: list[list[str]] = []
    for stats in ranked:
        if stats.kind != "procedure":
            eligible = "-"
        elif stats.name not in queries:
            eligible = "?"  # the feat does not declare this procedure
        elif queries[stats.name]:
            eligible = f"no (queries: {', '.join(queries[stats.name])})"
        elif declared[stats.name]:
            eligible = f"yes (read-only: {', '.join(declared[stats.name])})"
        else:
            eligible = "yes"
        timed = stats.durations
        rows.append(
            [
                stats.kind,
                stats.name,
                ",".join(sorted(stats.lanes)) or "-",
                str(stats.count),
                _fmt_ms(stats.total_ms) if timed else "-",
                _fmt_ms(stats.total_ms / len(timed)) if timed else "-",
                _fmt_ms(_percentile(timed, 0.50)) if timed else "-",
                _fmt_ms(_percentile(timed, 0.95)) if timed else "-",
                _fmt_ms(max(timed)) if timed else "-",
                str(stats.errors) if stats.errors else "-",
                str(stats.input_items) if stats.kind == "query" else "-",
                str(stats.result_items) if stats.kind == "query" else "-",
                (
                    ",".join(
                        f"{key}:{value}" for key, value in sorted(stats.access_patterns.items())
                    )
                    if stats.access_patterns
                    else "-"
                ),
                eligible,
            ]
        )
    if rows:
        headers = [
            "kind",
            "name",
            "lanes",
            "count",
            "total ms",
            "mean",
            "p50",
            "p95",
            "max",
            "errors",
            "input items",
            "result items",
            "access patterns",
            "pool-eligible",
        ]
        lines.extend(_table(headers, rows, numeric_from=3))
    else:
        lines.append("No procedure/projection/policy/query records in the capture.")
    lines.append("")

    # -- 3. Fan-out: what the stream grew by, and who reacted --------------------------
    lines.append("Events appended:")
    if profile.event_counts:
        width = max(len(n) for n in profile.event_counts)
        for name in sorted(profile.event_counts):
            lines.append(f"  {name.ljust(width)}  {profile.event_counts[name]}")
    else:
        lines.append("  (none)")
    lines.append("Policy invocations:")
    if profile.policy_counts:
        width = max(len(n) for n in profile.policy_counts)
        for name in sorted(profile.policy_counts):
            lines.append(f"  {name.ljust(width)}  {profile.policy_counts[name]}")
    else:
        lines.append("  (none)")
    lines.append("")

    # -- 4. The bottleneck statement ---------------------------------------------------
    serial, eligible_ms = bottleneck_totals(feat, profile, read_only)
    lines.append("Bottleneck:")
    lines.append(
        f"  serial-bound   {_fmt_ms(serial)} ms"
        "  (projections + policies + querying procedures — pinned to the fold lane)"
    )
    free = "query-free procedures" + (", and readers of read-only models" if read_only else "")
    lines.append(f"  pool-eligible  {_fmt_ms(eligible_ms)} ms  ({free})")
    for workers in _WORKER_COUNTS:
        lines.append(f"    / {workers} workers  {_fmt_ms(eligible_ms / workers)} ms")
    if serial == 0.0 and eligible_ms == 0.0:
        lines.append("  No element durations captured — nothing to bound.")
    elif serial >= eligible_ms / _WORKER_COUNTS[-1]:
        lines.append(
            "  Serial-bound work is the floor: even at "
            f"{_WORKER_COUNTS[-1]} workers the pool-eligible share falls below it, "
            "so the fold lane bounds this workload."
        )
    else:
        lines.append(
            "  Pool-eligible work dominates: worker count is the lever until its share "
            "falls under the serial-bound total."
        )
    return "\n".join(lines)
