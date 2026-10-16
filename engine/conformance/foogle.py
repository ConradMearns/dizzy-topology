"""foogle_shindig on the topology engine: the Simple Engine conformance scenario (spec §8).

    uv run --project <gen>/lib/python-uv \
        python engine/conformance/foogle.py <golden log.jsonl> [placement.json]

Run inside a generated foogle tree's environment (``just foogle`` builds one, and
``dizzy-topology vendor`` installs the engine into it), so the engine runs exactly as an
app gets it: vendored beside the wiring, ``dizzy_kit`` and ``dizzy_engine``.

- **No placement** (all inline): the log must equal DIZZY's golden byte for byte — the
  topology engine's inline lane IS the Simple Engine — including after a crash at each
  sampled claim, append or enqueue.
- **A placement with pools**: the log is no longer in the golden's ORDER (pool results
  land when they finish), so it must hold the golden's events and commands as a multiset
  — every derived fact, none extra, none missing — and the queries must read the same
  read model.

``gronk_values`` is reference data: nothing in the feature folds it, and the topology
declares it read-only (``read_only_models``). So it lives in its own SQLite file, seeded
once, and is only ever opened read-only — the host ATTACHes it under its in-memory read
model, and each pool worker opens its own read-only connection, which is what lets
``poke_snarf`` (whose one query reads it) run on a pool. A write to it raises; the run
checks that it does.
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import wiring
from dizzy_engine import CRASH_EXIT, dump
from gen_def.pydantic.commands import ThrowShindig
from gen_def.pydantic.query.get_foogle_progress import GetFoogleProgressInput
from gen_def.pydantic.query.get_shindig_progress import GetShindigProgressInput
from gen_def.sqla.models.foogle_progress import Base as FoogleProgressBase
from gen_def.sqla.models.gronk_values import Base as GronkValuesBase
from gen_def.sqla.models.gronk_values import GronkValue
from gen_def.sqla.models.shindig_progress import Base as ShindigProgressBase
from gen_int.python.adapters.sqla import SqlaAdapter
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from dizzy_topology_engine import Placement, TopologyEngine

FOOGLES = 10
EXPECTED_COMMANDS = 1024
EXPECTED_GRAND_TOTAL = 96871
CRASH_SAMPLE = (1, 2, 3, 4, 5, 700, 1501, 2222, 3070)
CRASH_ENV = "DIZZY_CRASH_AT"


def expected_snarf_count(i: int) -> int:
    return max(10, min(100, 10 + (i * 37) % 91))


def seed_reference(path: Path) -> Path:
    """Write the reference database: gronk_values, once, before any engine opens."""
    db = create_engine(f"sqlite:///{path}")
    GronkValuesBase.metadata.create_all(db)
    with Session(db) as session:
        session.add_all(GronkValue(snarf_id=str(i), value=i * 3 + 5) for i in range(100))
        session.commit()
    db.dispose()
    return path


def _read_only_url(reference: Path) -> str:
    return f"file:{reference}?mode=ro"


def pool_domains(reference: str) -> list:
    """A pool worker's tables. Its procedures read nothing the log folds (the build
    checked, rule 3), so it gets no read model — only its own read-only connection to
    the reference database, which is all ``poke_snarf`` queries."""
    db = create_engine(f"sqlite:///{_read_only_url(Path(reference))}&uri=true")
    resources = wiring.Resources(adapters={"sqla": SqlaAdapter(session=Session(db))})
    return [wiring.build_domain(resources)]


def drive(data: Path, placement: Placement | None, reference: Path) -> wiring.Queries:
    """A fresh, empty in-memory read model with the reference database ATTACHed
    read-only beneath it: SQLite resolves ``gronk_values`` to the attached file, since
    the main database has no such table."""

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", uri=True, check_same_thread=False)
        conn.execute("ATTACH DATABASE ? AS reference", (_read_only_url(reference),))
        return conn

    db = create_engine("sqlite://", creator=connect, poolclass=StaticPool)
    for base in (FoogleProgressBase, ShindigProgressBase):
        base.metadata.create_all(db)
    session = Session(db)
    resources = wiring.Resources(adapters={"sqla": SqlaAdapter(session=session)})
    crash_at = os.environ.get(CRASH_ENV)
    engine = TopologyEngine(
        data,
        [wiring.build_domain(resources)],
        placement,
        pool_domains=functools.partial(pool_domains, str(reference)),
        commit=session.commit,
        crash_at=int(crash_at) if crash_at else None,
    )
    engine.submit(ThrowShindig(shindig_id="s1", foogle_count=FOOGLES), id="throw_shindig/s1")
    engine.run()
    assert not engine.failures(), engine.failures()
    engine.close()
    try:  # read-only means read-only: the reference database refuses a write
        session.add(GronkValue(snarf_id="written", value=0))
        session.commit()
    except OperationalError as exc:
        assert "readonly" in str(exc), exc
        session.rollback()
    else:
        raise AssertionError("the reference database accepted a write")
    return wiring.build_queries(resources)


def check(data: Path, queries: wiring.Queries) -> str:
    shindig = queries.get_shindig_progress(GetShindigProgressInput(shindig_id="s1"))
    total = 0
    for i in range(FOOGLES):
        fp = queries.get_foogle_progress(GetFoogleProgressInput(shindig_id="s1", foogle_id=str(i)))
        assert fp.snarf_count == expected_snarf_count(i), (i, fp)
        assert fp.done and fp.snarfs_done == fp.snarf_count, (i, fp)
        total += fp.total_rating
    assert shindig.foogle_count == shindig.foogles_done == FOOGLES, shindig
    assert shindig.done is True
    assert shindig.grand_total == total == EXPECTED_GRAND_TOTAL, shindig.grand_total
    log = dump(data)
    done = sum('"status":"done"' in line for line in log.splitlines())
    assert done == EXPECTED_COMMANDS, done
    return log


def unordered(log: str) -> Counter:
    """The log as a multiset of facts: each line without its position."""
    lines = (json.loads(line) for line in log.splitlines())
    return Counter(
        json.dumps({k: v for k, v in obj.items() if k != "seq"}, sort_keys=True) for obj in lines
    )


def main() -> None:
    if sys.argv[1:2] == ["--crash-child"]:
        placement = Placement.load(sys.argv[4]) if len(sys.argv) > 4 else None
        drive(Path(sys.argv[2]), placement, Path(sys.argv[3]))
        sys.exit("the crash point was never reached")

    golden = Path(sys.argv[1]).read_text()
    placement = Placement.load(sys.argv[2]) if len(sys.argv) > 2 else None

    with tempfile.TemporaryDirectory() as tmp:
        reference = seed_reference(Path(tmp) / "reference.db")
        data = Path(tmp) / "clean"
        log = check(data, drive(data, placement, reference))
        print(f"ran {EXPECTED_COMMANDS} commands to idle, grand_total={EXPECTED_GRAND_TOTAL}")

        if placement is not None:
            pools = {
                n: lane.workers for n, lane in placement.lanes.items() if lane.executor == "pool"
            }
            assert unordered(log) == unordered(golden), "the pooled log's facts differ"
            print(f"pools {pools}: the log holds exactly the golden's facts")
            # Crash with pool work in flight: what was claimed and not yet appended is
            # 'running' in the store and re-runs on reopen — nothing lost, nothing twice.
            for k in CRASH_SAMPLE:
                data = Path(tmp) / f"pooled-crash-{k}"
                child = subprocess.run(
                    [
                        sys.executable,
                        __file__,
                        "--crash-child",
                        str(data),
                        str(reference),
                        sys.argv[2],
                    ],
                    env={**os.environ, CRASH_ENV: str(k)},
                )
                assert child.returncode == CRASH_EXIT, (k, child.returncode)
                recovered = check(data, drive(data, placement, reference))
                assert unordered(recovered) == unordered(golden), f"crash at {k}: facts differ"
            print(f"pooled crash runs {list(CRASH_SAMPLE)}: every log holds the golden's facts")
            print("ALL CHECKS PASSED")
            return

        assert log == golden, "the inline log differs from the golden"
        print("inline: log matches the Simple Engine golden byte for byte")
        for k in CRASH_SAMPLE:
            data = Path(tmp) / f"crash-{k}"
            child = subprocess.run(
                [sys.executable, __file__, "--crash-child", str(data), str(reference)],
                env={**os.environ, CRASH_ENV: str(k)},
            )
            assert child.returncode == CRASH_EXIT, (k, child.returncode)
            recovered = check(data, drive(data, None, reference))
            assert recovered == log, f"crash at {k}: the log differs"
        print(f"crash runs {list(CRASH_SAMPLE)}: every log matches")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
