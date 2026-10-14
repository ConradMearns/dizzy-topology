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
