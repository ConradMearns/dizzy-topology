"""The topology engine's telemetry (``dizzy_topology_engine.telemetry``): the Simple
Engine's one trace per run, with lanes, pools and workers in it, and each lane's queue
as up/down counters.

Read from the JSON-lines sinks (``<data>/telemetry/*.jsonl`` for spans,
``<data>/telemetry/metrics/*.jsonl`` for counter points); one test also stands up an
OTLP/HTTP receiver and checks the same spans and points arrive there, as
otel-desktop-viewer or a collector would get them.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from _app import Die, Fail, Square, Start, build_domain, pool_domains

from dizzy_topology_engine import Placement, TopologyEngine

SLACK_MS = 5.0  # clocks: a worker's wall clock against the host's, on one machine


def _placement(**commands: tuple[str, str]) -> Placement:
    lanes = {"main": {"executor": "inline"}, "cpu": {"executor": "pool", "workers": 2}}
    return Placement.parse(
        {
            "format": 1,
            "lanes": lanes,
            "default_lane": "main",
            "commands": {c: {"lane": lane, "priority": p} for c, (lane, p) in commands.items()},
        }
    )


def _spans(data: Path) -> list[dict]:
    return [
        json.loads(line)
        for path in sorted((data / "telemetry").glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def _points(data: Path) -> list[dict]:
    return [
        json.loads(line)
        for path in sorted((data / "telemetry" / "metrics").glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def _series(points: list[dict], name: str, lane: str) -> list[int]:
    return [
        p["value"] for p in points if p["name"] == name and p["attributes"]["dizzy.lane"] == lane
    ]


def _run(data: Path, placement: Placement, *commands) -> list[dict]:
    engine = TopologyEngine(data, [build_domain()], placement, pool_domains=pool_domains)
    for command in commands:
        engine.submit(command)
    engine.run()
    engine.close()
    return _spans(data)


def _tree_is_whole(spans: list[dict]) -> None:
    ids = {s["span_id"] for s in spans}
    assert len(ids) == len(spans), "a span id repeats"
    assert len({s["trace_id"] for s in spans}) == 1, "one trace per run"
    orphans = [s["name"] for s in spans if s["parent_id"] and s["parent_id"] not in ids]
    assert orphans == [], orphans
    assert [s["name"] for s in spans if not s["parent_id"]] == ["dizzy.run"]


def test_an_inline_unit_carries_its_lane(tmp_path: Path) -> None:
    spans = _run(tmp_path, _placement(start=("main", "interactive")), Start(n=3))
    _tree_is_whole(spans)
    units = [s for s in spans if s["name"] == "dizzy.unit"]
    assert len(units) == 4
    for unit in units:
        attrs = unit["attributes"]
        assert attrs["dizzy.lane"] == "main" and attrs["dizzy.executor"] == "inline"
        assert attrs["dizzy.queued_ms"] >= 0
        assert "dizzy.recovered" not in attrs
    priorities = {
        u["attributes"]["dizzy.element"]: u["attributes"]["dizzy.priority"] for u in units
    }
    assert priorities == {"start": "interactive", "square": "batch"}
    (run,) = [s for s in spans if s["name"] == "dizzy.run"]
    assert run["attributes"]["dizzy.lanes"] == "main=inlinex1, cpu=poolx2"


def test_a_pooled_unit_spans_claim_to_settle_with_the_workers_time_inside(
    tmp_path: Path,
) -> None:
    spans = _run(tmp_path, _placement(square=("cpu", "batch")), Start(n=6))
    _tree_is_whole(spans)
    by_id = {s["span_id"]: s for s in spans}
    pooled = [
        s for s in spans if s["name"] == "dizzy.unit" and s["attributes"]["dizzy.lane"] == "cpu"
    ]
    assert len(pooled) == 6
    workers = set()
    for unit in pooled:
        kids = sorted((s for s in spans if s["parent_id"] == unit["span_id"]), key=_start)
        names = [k["name"] for k in kids]
        assert names[:4] == [
            "dizzy.pool.wait",
            "dizzy.procedure square",
            "dizzy.pool.handoff",
            "dizzy.append",
        ], names
        procedure = kids[1]
        assert procedure["attributes"]["process.pid"] != os.getpid()
        workers.add(procedure["attributes"]["process.pid"])
        # The worker's time nests inside the unit, which runs from claim to settle.
        for kid in kids:
            assert _start(kid) >= _start(unit) - SLACK_MS
            assert _end(kid) <= _end(unit) + SLACK_MS
        assert unit["attributes"]["dizzy.procedure_ms"] == procedure["duration_ms"]

    (pool,) = [s for s in spans if s["name"] == "dizzy.pool cpu"]
    assert by_id[pool["parent_id"]]["name"] == "dizzy.run"
    attrs = pool["attributes"]
    assert attrs["dizzy.pool.jobs"] == 6 and attrs["dizzy.pool.failed"] == 0
    assert attrs["dizzy.pool.workers"] == 2 and attrs["dizzy.pool.started_workers"] == len(workers)
    started = [s for s in spans if s["name"] == "dizzy.worker"]
    assert {s["attributes"]["process.pid"] for s in started} == workers
    assert all(s["parent_id"] == pool["span_id"] for s in started)
    cold = [
        s for s in spans if s["name"] == "dizzy.pool.wait" and s["attributes"]["dizzy.pool.cold"]
    ]
    assert len(cold) == len(workers)  # each worker's first job waited on its start


def test_a_failing_pool_procedure_is_an_error_span(tmp_path: Path) -> None:
    spans = _run(tmp_path, _placement(fail=("cpu", "batch")), Fail(why="boom"))
    (procedure,) = [s for s in spans if s["name"] == "dizzy.procedure fail"]
    assert procedure["attributes"]["dizzy.outcome"] == "error"
    assert procedure["attributes"]["dizzy.error"] == "RuntimeError: boom"
    (pool,) = [s for s in spans if s["name"] == "dizzy.pool cpu"]
    assert pool["attributes"]["dizzy.pool.failed"] == 1


def test_a_dying_worker_breaks_its_pool_span(tmp_path: Path) -> None:
    spans = _run(tmp_path, _placement(die=("cpu", "batch")), Die())
    _tree_is_whole(spans)
    (pool,) = [s for s in spans if s["name"] == "dizzy.pool cpu"]
    assert pool["attributes"]["dizzy.outcome"] == "error"
    assert pool["attributes"]["dizzy.pool.broken"] is True
    (unit,) = [s for s in spans if s["name"] == "dizzy.unit"]
    assert unit["attributes"]["dizzy.outcome"] == "error"
    assert not [s for s in spans if s["name"].startswith("dizzy.procedure")]  # it never finished


def test_a_rerun_of_an_interrupted_attempt_says_so(tmp_path: Path) -> None:
    placement = _placement(square=("cpu", "batch"))
    engine = TopologyEngine(tmp_path, [build_domain()], placement, pool_domains=pool_domains)
    engine.submit(Square(i=7))
    engine._claim_next()  # claimed durably, never run: a crash right here
    engine.close()
    spans = _run(tmp_path, placement)
    (unit,) = [s for s in spans if s["name"] == "dizzy.unit"]
    assert unit["attributes"]["dizzy.recovered"] is True


def test_each_lane_counts_its_queue_up_and_down(tmp_path: Path) -> None:
    spans = _run(tmp_path, _placement(square=("cpu", "batch")), Start(n=6))
    points = _points(tmp_path)
    queued, in_flight = (
        {lane: _series(points, name, lane) for lane in ("main", "cpu")}
        for name in ("dizzy.lane.queued", "dizzy.lane.in_flight")
    )
    # start: queued, claimed, run inline; it dispatches six squares to the pool.
    assert queued["main"] == [1, 0]
    assert in_flight["main"] == [1, 0]
    assert queued["cpu"][:6] == [1, 2, 3, 4, 5, 6] and queued["cpu"][-1] == 0
    assert in_flight["cpu"][-1] == 0
    assert max(in_flight["cpu"]) == 2  # the lane's width: never more in flight than workers
    assert min(v for series in (*queued.values(), *in_flight.values()) for v in series) == 0
    # Each step is one command moving: claims and settles match the units traced.
    claims = sum(1 for a, b in itertools.pairwise([0, *in_flight["cpu"]]) if b > a)
    assert claims == len([s for s in spans if s["name"] == "dizzy.unit"]) - 1 == 6
    attrs = next(p["attributes"] for p in points if p["attributes"]["dizzy.lane"] == "cpu")
    assert attrs == {"dizzy.domain": "squares", "dizzy.lane": "cpu", "dizzy.priority": "batch"}


def test_an_open_counts_what_is_still_waiting(tmp_path: Path) -> None:
    placement = _placement(square=("cpu", "batch"))
    engine = TopologyEngine(
        tmp_path, [build_domain()], placement, pool_domains=pool_domains, telemetry=False
    )
    for i in range(3):
        engine.submit(Square(i=i))
    engine._claim_next()  # one claimed durably, never run: a crash right here
    engine.close()
    _run(tmp_path, placement)
    points = _points(tmp_path)
    # The interrupted attempt waits to re-run, so the reopened lane starts at three.
    assert _series(points, "dizzy.lane.queued", "cpu")[0] == 3
    assert _series(points, "dizzy.lane.queued", "cpu")[-1] == 0


def test_a_retry_queues_again(tmp_path: Path) -> None:
    placement = _placement(fail=("cpu", "batch"))
    engine = TopologyEngine(tmp_path, [build_domain()], placement, pool_domains=pool_domains)
    command_id = engine.submit(Fail(why="boom"))
    engine.run()
    engine.retry(command_id)
    engine.run()
    engine.close()
    assert _series(_points(tmp_path), "dizzy.lane.queued", "cpu") == [1, 0, 1, 0]


def test_the_same_spans_reach_an_otlp_receiver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: dict[str, list[dict]] = {"/v1/traces": [], "/v1/metrics": []}

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — the stdlib's name
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received[self.path].append(json.loads(body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    try:
        written = _run(tmp_path, _placement(square=("cpu", "batch")), Start(n=4))
    finally:
        server.shutdown()
    sent = [
        span
        for payload in received["/v1/traces"]
        for resource in payload["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    assert sorted(s["spanId"] for s in sent) == sorted(s["span_id"] for s in written)
    procedures = [s for s in sent if s["name"] == "dizzy.procedure square"]
    assert len(procedures) == 4
    attrs = {a["key"]: a["value"] for a in procedures[0]["attributes"]}
    assert int(attrs["process.pid"]["intValue"]) != os.getpid()

    metrics = [
        metric
        for payload in received["/v1/metrics"]
        for resource in payload["resourceMetrics"]
        for scope in resource["scopeMetrics"]
        for metric in scope["metrics"]
    ]
    assert {m["name"] for m in metrics} == {"dizzy.lane.queued", "dizzy.lane.in_flight"}
    for metric in metrics:  # an UpDownCounter: a cumulative sum that may go down
        assert metric["sum"]["isMonotonic"] is False
        assert metric["sum"]["aggregationTemporality"] == 2
    sent_points = [p for m in metrics for p in m["sum"]["dataPoints"]]
    assert len(sent_points) == len(_points(tmp_path))


def _start(span: dict) -> float:
    return datetime.fromisoformat(span["start"]).timestamp() * 1000


def _end(span: dict) -> float:
    return _start(span) + span["duration_ms"]
