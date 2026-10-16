"""spec/specs/engine.qnt drives the real TopologyEngine: model-based testing (``_mbt.py``).

Quint generates traces of the spec; each is replayed on a real engine, and after every
step the engine's store and memory, abstracted to the spec's variables, must equal the
trace's next state. Where ``test_spec_conformance`` checks that the engine's END states
satisfy the spec's invariants, this checks that every STEP the engine takes is the step
the spec took — claim order, which job a pool commits, what a crash leaves, what a reopen
refolds — so a behaviour the spec does not have fails at the step where it appears.

Traces come from three instances of the spec: ``engine`` (what is proved; a two-wide
pool), ``engineNarrow`` (a one-wide pool and a third submission, so a full pool with pool
work queued behind it is common — where a lane's capacity is tested), and ``engineReopen``
sampled from ``initCrossStore`` with ``reopenStep`` (a model fed by both stores, reopened
after it has folded — where a reopen reorders a fold, and the replay checks the real
engine reorders it exactly as the spec does).

A failure names its instance and trace: ``pytest -k 'engine-trace_17'``. The traces are
regenerated from a fixed seed on every run, so a spec change is picked up without a stale
copy.
"""

from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from _mbt import load_trace, replay

QUINT = shutil.which("quint")
SPEC = Path(__file__).resolve().parents[2] / "spec" / "specs" / "engine.qnt"
INSTANCES = {"engine": 2, "engineNarrow": 1, "engineReopen": 1}  # main -> its POOL_WORKERS
BIAS = {"engineReopen": ["--init=initCrossStore", "--step=reopenStep"]}
TRACES = {"engine": 150, "engineNarrow": 150, "engineReopen": 400}
STEPS = 30
SEED = 1


@pytest.fixture(scope="module")
def traces(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[list[dict]]]:
    if QUINT is None:
        pytest.skip("quint is not installed (npm i -g @informalsystems/quint)")
    return {main: _generate(tmp_path_factory.mktemp(main), main) for main in INSTANCES}


def _generate(out: Path, main: str) -> list[list[dict]]:
    subprocess.run(
        [
            QUINT,
            "run",
            SPEC.name,
            f"--main={main}",
            *BIAS.get(main, []),
            "--mbt",
            "--invariant=allInvariants",
            f"--max-samples={TRACES[main]}",
            f"--n-traces={TRACES[main]}",
            f"--max-steps={STEPS}",
            f"--seed={SEED}",
            f"--out-itf={out}/t{{seq}}.itf.json",
        ],
        cwd=SPEC.parent,
        check=True,
        capture_output=True,
    )
    return [load_trace(out / f"t{n}.itf.json") for n in range(TRACES[main])]


@pytest.mark.parametrize(
    ("main", "n"),
    [(main, n) for main in INSTANCES for n in range(TRACES[main])],
    ids=lambda v: v if isinstance(v, str) else f"trace_{v}",
)
def test_the_engine_takes_every_step_the_spec_takes(
    traces: dict[str, list[list[dict]]], main: str, n: int, tmp_path: Path
) -> None:
    replay(traces[main][n], tmp_path, INSTANCES[main])


@pytest.mark.parametrize("main", INSTANCES)
def test_the_traces_exercise_every_action(traces: dict[str, list[list[dict]]], main: str) -> None:
    """Coverage of the replay is the traces' coverage: every action of the spec must
    occur, or the replay above says nothing about it."""
    seen = Counter(s["mbt::actionTaken"] for t in traces[main] for s in t)
    actions = {
        "submit", "claim", "runInline", "failInline", "completePool", "failPool",
        "retry", "settleFold", "enqueue", "advance", "crash",
    }  # fmt: skip
    if main == "engineReopen":  # MAX_FAILS = 0 there: nothing fails, nothing to retry
        actions -= {"failInline", "failPool", "retry"}
    assert actions <= set(seen), f"never taken: {sorted(actions - set(seen))}"


@pytest.mark.parametrize("main", INSTANCES)
def test_the_traces_exercise_several_stores(traces: dict[str, list[list[dict]]], main: str) -> None:
    """engine.qnt's witnessTwoStores and witnessCrossDomain, among the replayed traces:
    events in both domains' stores, and a policy of one domain dispatching a command the
    other domain's procedure ran — so the replay checks the stores' interleaving and the
    per-store cursors, not just one store's."""
    two_stores = cross = 0
    for trace in traces[main]:
        last = trace[-1]
        domain_of = last["g"]["domainOf"]
        stores = {domain_of[e["cause"]["target"]] for e in last["log"]}
        two_stores += len(stores) > 1
        cross += any(
            e["cause"]["origin"] != "host"
            and domain_of[e["cause"]["origin"]] != domain_of[e["cause"]["target"]]
            for e in last["log"]
        )
    assert two_stores >= 10 and cross >= 10, (two_stores, cross)


def test_the_replay_drives_the_engine_through_reordering_reopens(
    traces: dict[str, list[list[dict]]],
) -> None:
    """engine.qnt's reopen harm, on the real engine: among the replayed traces, reopens
    that refold a model out of append order. The replay compares fold order after every
    step, so the engine reorders exactly as the spec does. (A decision taken on one is
    rarer still — about one trace in 500 — and is replayed by the scripted scenario's
    twin, test_spec_conformance's test_a_reopen_reorders_a_model_fed_by_two_stores.)"""

    def reordered(state: dict) -> bool:
        return any(list(seq) != sorted(seq) for seq in state["foldSeq"].values())

    reorders = sum(any(reordered(s) for s in t) for t in traces["engineReopen"])
    assert reorders >= 5, reorders  # 10 at this seed
