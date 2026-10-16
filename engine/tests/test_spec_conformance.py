"""The real topology engine, held to spec/specs/engine.qnt's invariants.

The model checker proves the invariants of the DESIGN over every small graph; these
tests check the IMPLEMENTATION satisfies the same invariants (ported in ``_spec.py``)
over random graphs from the same universe — random rule-3 placements, priorities and
pool widths, and real crashes at random claim/append/enqueue points (``crash_at``, in a
child process, then a reopen that drains the store).

Crashes are swept, not sampled: a crash between two particular transactions (say, an
append and its ack) is one point among dozens, and a random point misses it. So every
claim, append and enqueue of a scenario is crashed at in turn, in-process, then drained;
one more test crashes a real child process.

A failure names its seed: ``pytest -k 'seed_17'`` reproduces it exactly.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from _graph_app import Crash, drive, random_scenario
from _spec import Store, check, check_idle

SEEDS = range(40)
SWEEP_SEEDS = {False: range(30), True: range(12)}  # pooled runs spawn a pool per crash point
CRASH_SEEDS = range(10)
TESTS = Path(__file__).parent


def _assert(errors: list[str], seed: int, scenario, when: str = "") -> None:
    assert not errors, f"seed {seed} {when}: {scenario.dumps()}\n" + "\n".join(errors)


@pytest.mark.parametrize("pools", [False, True], ids=["inline", "pooled"])
@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed_{s}")
def test_a_clean_run_satisfies_the_spec(tmp_path: Path, seed: int, pools: bool) -> None:
    scenario = random_scenario(random.Random(seed), pools)
    model, _ = drive(tmp_path, scenario)
    _assert(check_idle(scenario.graph, Store.read(tmp_path), model), seed, scenario)


@pytest.mark.parametrize(
    ("pools", "seed"),
    [(pools, seed) for pools in (False, True) for seed in SWEEP_SEEDS[pools]],
    ids=lambda v: (
        {False: "inline", True: "pooled"}.get(v, f"seed_{v}")
        if isinstance(v, bool)
        else f"seed_{v}"
    ),
)
def test_a_crash_at_every_point_satisfies_the_spec(tmp_path: Path, seed: int, pools: bool) -> None:
    """For every crash point k: crash there, check the store as left, reopen (crashing
    once more at a random later point), drain, check again."""
    rng = random.Random(2000 + seed)
    scenario = random_scenario(rng, pools)
    _, points = drive(tmp_path / "clean", scenario)
    for k in range(1, points + 1):
        data = tmp_path / f"crash-{k}"
        with pytest.raises(Crash):
            drive(data, scenario, crash_after=k)
        _assert(check(scenario.graph, Store.read(data)), seed, scenario, f"crashed at {k}")
        try:
            drive(data, scenario, crash_after=rng.randint(1, points))
        except Crash:
            _assert(check(scenario.graph, Store.read(data)), seed, scenario, f"twice, {k}")
        model, _ = drive(data, scenario)
        errors = check_idle(scenario.graph, Store.read(data), model)
        _assert(errors, seed, scenario, f"drained after a crash at {k}")


@pytest.mark.parametrize("pools", [False, True], ids=["inline", "pooled"])
@pytest.mark.parametrize("seed", CRASH_SEEDS, ids=lambda s: f"seed_{s}")
def test_a_real_process_crash_still_satisfies_the_spec(
    tmp_path: Path, seed: int, pools: bool
) -> None:
    """Crash at a random claim, append or enqueue; the store as the crash left it must
    already satisfy the spec, and so must the store a reopen drains it to."""
    rng = random.Random(1000 + seed)
    scenario = random_scenario(rng, pools)
    _, points = drive(tmp_path / "clean", scenario)
    if points == 0:
        pytest.skip("the scenario has no crash point")
    crash_at = rng.randint(1, points)

    data = tmp_path / "crashed"
    spec_file = tmp_path / "scenario.json"
    spec_file.write_text(scenario.dumps())
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(TESTS), *sys.path])}
    child = subprocess.run(
        [sys.executable, str(TESTS / "_graph_app.py"), str(spec_file), str(data), str(crash_at)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 86, child.stderr  # dizzy_engine.CRASH_EXIT
    _assert(check(scenario.graph, Store.read(data)), seed, scenario)

    model, _ = drive(data, scenario)
    _assert(check_idle(scenario.graph, Store.read(data), model), seed, scenario)


def test_the_seeds_reach_a_pooled_reader_of_a_read_only_model(tmp_path: Path) -> None:
    """engine.qnt's witnessPooledReadOnly, on the real engine: the clean pooled seeds
    must commit blocks of pooled procedures that read a read-only model — each from its
    worker's own seeded copy — or the conformance runs above prove nothing about them."""
    reached = 0
    for seed in SEEDS:
        scenario = random_scenario(random.Random(seed), True)
        g = scenario.graph
        commands = scenario.placement["commands"]
        if not any(g.reads[p] and commands[p]["lane"] == "pool" for p in g.procs):
            continue
        drive(tmp_path / str(seed), scenario)
        store = Store.read(tmp_path / str(seed))
        reached += any(
            commands[store.commands[e.command_id].proc]["lane"] == "pool" and e.view
            for e in store.log
        )
    assert reached >= 3, f"only {reached} seeds ran a pooled read-only reader"


def _harm_scenario(domain_of: dict[str, str]):
    """engine.qnt's HARM_GRAPH: p2 emits e1; p1 emits e1 then e2; m1 folds e1; y1 reacts
    to e2, reads m1 and dispatches p2. The host submits p2, then p1."""
    from _graph_app import Graph, Scenario

    graph = Graph(
        procs=["p1", "p2"],
        policies=["y1"],
        models=["m1"],
        kinds=["e1", "e2"],
        reads={"p1": [], "p2": [], "y1": ["m1"]},
        emits={"p1": ["e1", "e2"], "p2": ["e1"]},
        folds={"m1": ["e1"]},
        reacts={"y1": ["e2"]},
        dispatches={"y1": ["p2"]},
        interactive=[],
        read_only=[],
        domains=["d1", "d2"],
        domain_of=domain_of,
    )
    lanes = {"main": {"executor": "inline", "workers": 1}}
    placement = {"format": 1, "lanes": lanes, "default_lane": "main", "commands": {}}
    return Scenario(graph, placement, ["p2", "p1"])


@pytest.mark.parametrize(
    ("p2_domain", "reordered"), [("d2", True), ("d1", False)], ids=["two_stores", "one_store"]
)
def test_a_reopen_reorders_a_model_fed_by_two_stores(
    tmp_path: Path, p2_domain: str, reordered: bool
) -> None:
    """engine.qnt's reopenReordersTest / oneStoreKeepsOrderTest, on the real engine.

    Crash at y1's enqueue while settling p1's e2 (crash point 7: two submits, then claim,
    append, claim, append, enqueue): p1's e1 is settled, its e2 is not. The reopen refolds
    the core store (d1) first, then the plugin's — so m1, fed by p2 in d2 and p1 in d1,
    comes back as [p1's e1, p2's e1], out of append order, and y1 re-settles e2 reading it.
    With both procedures in one store the refold is in append order."""
    scenario = _harm_scenario({"p1": "d1", "p2": p2_domain, "y1": "d1"})
    with pytest.raises(Crash):
        drive(tmp_path, scenario, crash_after=7)
    store = Store.read(tmp_path)
    settled = [store.settled(e) for e in store.log]
    assert [e.kind for e in store.log] == ["e1", "e1", "e2"] and settled == [True, True, False]

    model, _ = drive(tmp_path, scenario)  # reopen and drain
    pos = {e.tag: e.pos for e in Store.read(tmp_path).log}
    m1 = [pos[t] for t in model["m1"]]
    assert m1[:2] == ([1, 0] if reordered else [0, 1]), m1
    assert (
        check_idle(scenario.graph, Store.read(tmp_path), model) == []
    )  # rule 7 is about decisions
