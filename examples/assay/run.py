"""Run the assay pipeline on the topology engine — the host an app writes.

One ``ingest_run`` goes in. Policies fan it out across the lanes ``placement.json``
names, and back in to a single summary:

    ingest_run ─▶ run_parsed ─┬─▶ split_run ─▶ sample_extracted × N   (main)
                              └─▶ render_plot ─▶ plot_rendered        (compute, pool×4)
    sample_extracted ─┬─▶ analyze_sample ─▶ sample_analyzed           (compute, pool×4)
                      └─▶ classify_sample ─▶ sample_classified        (classify, pool×1)
    last result home  ─▶ summarize_run ─▶ run_summarized              (main)

Run inside the app's generated workspace, after ``dizzy-topology vendor`` installed the
engine there (README.md has the whole sequence):

    uv run --project <app>/lib/python-uv python run.py --placement <app>/placement.json

Without ``--placement`` every command runs inline: the Simple Engine's own order. The
summary must come out the same either way. Only the wall clock should differ.

What a host owns, and nothing else: the read model (an in-memory SQLite for the ledger,
with the reference panel ATTACHed read-only beneath it), the data directory, what a
pool worker builds (``pool_domains``), and the lifecycle.
"""

from __future__ import annotations

import argparse
import functools
import sqlite3
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

import wiring
from gen_def.pydantic.commands import IngestRun
from gen_def.pydantic.query.get_run_results import GetRunResultsInput
from gen_def.sqla.models.ledger import Base as LedgerBase
from gen_def.sqla.models.ledger import RunSummary
from gen_def.sqla.models.panel import Base as PanelBase
from gen_def.sqla.models.panel import Cutoff
from gen_int.python.adapters.sqla import SqlaAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from dizzy_topology_engine import Placement, TopologyEngine

RUN_ID = "run-001"
CUTOFFS = {"hit": Decimal("0.5"), "miss": Decimal("0")}  # the default reference panel


def write_manifest(path: Path, samples: int) -> list[Decimal]:
    """One `sample_id,measurement` line per sample — the instrument's output.
    Deterministic, so reruns are comparable. Returns the measurements."""
    values = [Decimal(str(round((i * 0.377 + 0.113) % 1.0, 6))) for i in range(1, samples + 1)]
    lines = [f"{RUN_ID}-s{i:03d},{v}" for i, v in enumerate(values, start=1)]
    path.write_text("\n".join(lines) + "\n")
    return values


def seed_panel(path: Path) -> None:
    """The reference panel: written once, before any engine opens, then only read."""
    db = create_engine(f"sqlite:///{path}")
    PanelBase.metadata.create_all(db)
    with Session(db) as session:
        session.add_all(
            Cutoff(cutoff_id=f"default/{label}", panel="default", label=label, min_score=score)
            for label, score in CUTOFFS.items()
        )
        session.commit()
    db.dispose()


def _read_only(path: Path) -> str:
    return f"file:{path}?mode=ro"


def pool_domains(panel: str) -> list:
    """What each pool worker builds, once, in its own process. Module-level so it pickles
    by reference. A worker never sees the host's read model. Its procedures read nothing
    the log folds (rule 3), so it needs only its own read-only connection to the panel,
    which classify_sample queries."""
    db = create_engine(f"sqlite:///{_read_only(Path(panel))}&uri=true")
    resources = wiring.Resources(adapters={"sqla": SqlaAdapter(session=Session(db))})
    return [wiring.build_domain(resources)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", type=int, default=24, help="samples in the run")
    parser.add_argument("--placement", type=Path, help="placement.json (default: all inline)")
    parser.add_argument("--data", type=Path, help="data directory (default: a fresh temp dir)")
    args = parser.parse_args(argv)

    workdir = args.data or Path(tempfile.mkdtemp(prefix="assay-"))
    workdir.mkdir(parents=True, exist_ok=True)
    measurements = write_manifest(workdir / f"{RUN_ID}.csv", args.samples)
    panel = workdir / "panel.db"
    if not panel.exists():
        seed_panel(panel)

    # The read model: the ledger in memory, the panel ATTACHed read-only beneath it.
    # SQLite resolves the panel table to the attached file: main has no such table.
    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", uri=True, check_same_thread=False)
        conn.execute("ATTACH DATABASE ? AS reference", (_read_only(panel),))
        return conn

    db = create_engine("sqlite://", creator=connect, poolclass=StaticPool)
    LedgerBase.metadata.create_all(db)
    session = Session(db)
    resources = wiring.Resources(adapters={"sqla": SqlaAdapter(session=session)})

    placement = Placement.load(args.placement) if args.placement else None
    engine = TopologyEngine(
        workdir / "store",
        [wiring.build_domain(resources)],
        placement,
        pool_domains=functools.partial(pool_domains, str(panel)),
        commit=session.commit,
    )
    started = time.perf_counter()
    engine.submit(
        IngestRun(run_id=RUN_ID, input_path=str(workdir / f"{RUN_ID}.csv")), id=f"ingest/{RUN_ID}"
    )
    units = engine.run()
    elapsed = time.perf_counter() - started
    failures = engine.failures()
    engine.close()

    lanes = (
        ", ".join(f"{n} {lane.executor}×{lane.workers}" for n, lane in placement.lanes.items())
        if placement
        else "all inline"
    )
    print(f"lanes:   {lanes}")
    print(f"ran:     {units} units in {elapsed:.2f}s  (data: {workdir})")
    if failures:
        print(f"FAILED:  {failures}")
        return 1

    summary = session.get(RunSummary, RUN_ID)
    results = wiring.build_queries(resources).get_run_results(GetRunResultsInput(run_id=RUN_ID))
    hits = sum(1 for m in measurements if m % 1 >= CUTOFFS["hit"])
    print(
        f"summary: {summary.analyzed_count} analyzed, {summary.classified_count} classified, "
        f"{summary.hit_count} hits, mean metric {summary.mean_metric}"
    )
    assert summary.sample_count == summary.analyzed_count == args.samples, summary
    assert summary.classified_count == args.samples, summary
    assert summary.hit_count == hits, (summary.hit_count, hits)
    assert results.labels == [("hit" if m % 1 >= CUTOFFS["hit"] else "miss") for m in measurements]
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
