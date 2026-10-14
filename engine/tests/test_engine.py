import os
import sqlite3
from pathlib import Path

import pytest
from _app import Fail, Slow, Square, Start, WhoAmI, build_domain, pool_domains
from dizzy_engine import Engine, dump

from dizzy_topology_engine import Placement, PlacementError, TopologyEngine

N = 40
TOTAL = sum(i * i for i in range(N))


_LANES = {
    "main": {"executor": "inline"},
    "cpu": {"executor": "pool", "workers": 2},
    "solo": {"executor": "pool", "workers": 1},
}


def _placement(**commands: tuple[str, str]) -> Placement:
    """``main`` plus whichever pool lanes the placed commands use."""
    used = {"main"} | {lane for lane, _ in commands.values()}
    return Placement.parse(
        {
            "format": 1,
            "lanes": {name: lane for name, lane in _LANES.items() if name in used},
            "default_lane": "main",
            "commands": {c: {"lane": lane, "priority": p} for c, (lane, p) in commands.items()},
        }
    )


def _rows(data: Path) -> list[tuple]:
    """Every store row that is not a timestamp — ids included, so 'same log' means the
    same derived ids, not just the same shape."""
    db = sqlite3.connect(data / "squares.db")
    return [
        *db.execute("SELECT seq, id, type, payload, command_id, attempt_id FROM events"),
        *db.execute("SELECT seq, id, type, payload, origin, cause_event, status FROM commands"),
        *db.execute("SELECT id, command_id, attempt_no, status, reason, error FROM attempts"),
        *db.execute("SELECT * FROM cursors"),
    ]


def _status_counts(data: Path) -> dict[str, int]:
    db = sqlite3.connect(data / "squares.db")
    return dict(db.execute("SELECT status, count(*) FROM commands GROUP BY status"))


# --- the inline lane is the Simple Engine ------------------------------------------------


def test_all_inline_writes_the_simple_engines_log_exactly(tmp_path: Path) -> None:
    simple_model: dict = {}
    simple = Engine(tmp_path / "simple", [build_domain(simple_model)], telemetry=False)
    simple.submit(Start(n=N))
    simple.run()
    simple.close()

    model: dict = {}
    engine = TopologyEngine(tmp_path / "topo", [build_domain(model)], telemetry=False)
    engine.submit(Start(n=N))
    engine.run()
    engine.close()

    assert _rows(tmp_path / "topo") == _rows(tmp_path / "simple")
    assert dump(tmp_path / "topo") == dump(tmp_path / "simple")
    assert model == simple_model == {"total": TOTAL, "count": N}


# --- pools ---------------------------------------------------------------------------


def test_pool_lane_runs_in_a_child_and_the_parent_writes(tmp_path: Path) -> None:
    model: dict = {}
    engine = TopologyEngine(
        tmp_path,
        [build_domain(model)],
        _placement(square=("cpu", "batch"), who_am_i=("cpu", "batch")),
        pool_domains=pool_domains,
        telemetry=False,
    )
    engine.submit(WhoAmI(tag="pooled"))
    engine.submit(Start(n=N))
    engine.run()
    engine.close()

    assert model["total"] == TOTAL and model["count"] == N
    assert model["pids"]["pooled"] != os.getpid()
    assert _status_counts(tmp_path) == {"done": N + 2}


def test_a_pooled_store_opens_in_the_simple_engine_with_nothing_left_to_do(
    tmp_path: Path,
) -> None:
    engine = TopologyEngine(
        tmp_path,
        [build_domain()],
        _placement(square=("cpu", "batch")),
        pool_domains=pool_domains,
        telemetry=False,
    )
    engine.submit(Start(n=N))
    engine.run()
    engine.close()

    model: dict = {}
    simple = Engine(tmp_path, [build_domain(model)], telemetry=False)  # refolds on open
    assert simple.run() == 0
    simple.close()
    assert model == {"total": TOTAL, "count": N}


def test_a_slow_pool_procedure_does_not_hold_the_head_of_the_queue(tmp_path: Path) -> None:
    engine = TopologyEngine(
        tmp_path,
        [build_domain()],
        _placement(slow=("solo", "batch")),
        pool_domains=pool_domains,
        telemetry=False,
    )
    engine.submit(Slow(ms=1500))
    engine.submit(Start(n=5))
    engine.run()
    engine.close()

    db = sqlite3.connect(tmp_path / "squares.db")
    order = [t for (t,) in db.execute("SELECT type FROM events ORDER BY seq")]
    # Enqueued first, appended last: the inline lane drained around it.
    assert order[-1] == "slow_done"
    assert order.count("squared") == 5


def test_a_failing_pool_procedure_fails_its_attempt_and_retries_on_request(
    tmp_path: Path,
) -> None:
    engine = TopologyEngine(
        tmp_path,
        [build_domain()],
        _placement(fail=("cpu", "batch")),
        pool_domains=pool_domains,
        telemetry=False,
    )
    command_id = engine.submit(Fail(why="boom"))
    engine.run()
    assert engine.status(command_id) == "failed"
    assert engine.failures() == [(command_id, "fail", "RuntimeError: boom")]

    engine.retry(command_id)  # never automatic (Simple Engine spec §11.2)
    engine.run()
    engine.close()
    db = sqlite3.connect(tmp_path / "squares.db")
    assert db.execute("SELECT count(*) FROM attempts").fetchone() == (2,)


def test_an_interrupted_pool_attempt_reruns_under_its_own_id(tmp_path: Path) -> None:
    placement = _placement(square=("cpu", "batch"))
    engine = TopologyEngine(
        tmp_path, [build_domain()], placement, pool_domains=pool_domains, telemetry=False
    )
    engine.submit(Square(i=7))
    lane, job = engine._claim_next()  # claimed durably, never run: a crash right here
    assert lane.name == "cpu"
    engine.close()

    model: dict = {}
    engine = TopologyEngine(
        tmp_path, [build_domain(model)], placement, pool_domains=pool_domains, telemetry=False
    )
    assert engine.run() == 1
    engine.close()
    assert model == {"total": 49, "count": 1}
    db = sqlite3.connect(tmp_path / "squares.db")
    assert db.execute("SELECT id, status FROM attempts").fetchall() == [(job[6], "done")]


# --- claim order ---------------------------------------------------------------------


def test_interactive_commands_are_claimed_before_batch(tmp_path: Path) -> None:
    engine = TopologyEngine(
        tmp_path,
        [build_domain()],
        _placement(who_am_i=("main", "interactive")),
        telemetry=False,
    )
    for i in range(3):
        engine.submit(Square(i=i))
    engine.submit(WhoAmI(tag="urgent"))
    engine.run()
    engine.close()

    db = sqlite3.connect(tmp_path / "squares.db")
    order = [t for (t,) in db.execute("SELECT type FROM events ORDER BY seq")]
    assert order == ["ran", "squared", "squared", "squared"]


# --- placement -----------------------------------------------------------------------


def test_a_pool_lane_needs_pool_domains(tmp_path: Path) -> None:
    with pytest.raises(PlacementError, match="pool_domains"):
        TopologyEngine(tmp_path, [build_domain()], _placement(square=("cpu", "batch")))


def test_a_placement_for_another_feature_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PlacementError, match="no domain handles"):
        TopologyEngine(
            tmp_path, [build_domain()], _placement(launch_rocket=("main", "batch")), telemetry=False
        )


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ({"format": 2, "lanes": {"main": {}}}, "format"),
        ({"format": 1, "lanes": {}}, "no lanes"),
        ({"format": 1, "lanes": {"main": {"executor": "gpu"}}}, "executor"),
        ({"format": 1, "lanes": {"main": {"workers": 0}}}, "workers"),
        ({"format": 1, "lanes": {"main": {}}, "default_lane": "x"}, "default_lane"),
        ({"format": 1, "lanes": {"main": {}}, "commands": {"c": {"lane": "x"}}}, "not declared"),
    ],
)
def test_malformed_placements_are_refused(doc: dict, message: str) -> None:
    with pytest.raises(PlacementError, match=message):
        Placement.parse(doc)


# --- process hygiene -----------------------------------------------------------------

_HOST = """
import sys, time
sys.path.insert(0, {tests!r})
from _app import Slow, build_domain, pool_domains
from dizzy_topology_engine import Placement, TopologyEngine
placement = Placement.parse({{"format": 1, "lanes": {{"main": {{}},
    "solo": {{"executor": "pool"}}}}, "commands": {{"slow": {{"lane": "solo"}}}}}})
engine = TopologyEngine({data!r}, [build_domain()], placement, pool_domains=pool_domains,
                        telemetry=False)
engine.submit(Slow(ms=60_000))
engine._dispatch(*engine._claim_next())
print(*engine._pools["solo"]._processes, flush=True)
time.sleep(60)
"""


def test_pool_workers_die_with_a_killed_host(tmp_path: Path) -> None:
    """A crashed host never shuts its pools down; its workers must not outlive it."""
    import signal
    import subprocess
    import sys
    import time

    host = subprocess.Popen(
        [sys.executable, "-c", _HOST.format(tests=str(Path(__file__).parent), data=str(tmp_path))],
        stdout=subprocess.PIPE,
        text=True,
    )
    workers = [int(pid) for pid in host.stdout.readline().split()]
    assert workers, "the host spawned no pool worker"
    host.send_signal(signal.SIGKILL)
    host.wait()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(Path(f"/proc/{p}").exists() for p in workers):
        time.sleep(0.1)
    alive = [p for p in workers if Path(f"/proc/{p}").exists()]
    for pid in alive:
        os.kill(pid, signal.SIGKILL)
    assert not alive, f"pool workers {alive} outlived their host"
