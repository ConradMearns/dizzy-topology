"""The topology engine: the Simple Engine, with lanes.

A placement (``dizzy-topology place``) puts each command on a lane. An ``inline`` lane is
served on the calling thread exactly as the Simple Engine serves its one queue — it IS the
Simple Engine's own unit, inherited, not reimplemented. A ``pool`` lane runs its procedures
in a spawned process pool, so a slow procedure no longer holds the head of the queue.

**One writer.** Only the thread that calls :meth:`run` touches a store. A pool child runs
one procedure and returns what it emitted (:mod:`dizzy_topology_engine.pool`); the parent
appends it, settles it (fold, commit, policies, enqueue, cursor) and acks it, in that
order, in the same transaction shape as the Simple Engine. Nothing in the store format
changes, so a store this engine wrote opens in any Simple Engine and vice versa.

**Claim order.** Interrupted attempts first (re-run under the same attempt id, spec §4),
then ``interactive`` commands before ``batch``, then enqueue order — among the lanes with
room. With no pool lanes and no interactive commands that is exactly the Simple Engine's
FIFO, and the log is byte-for-byte the Simple Engine's.

**What pools cost.** A pool procedure runs concurrently with the inline lane, so where its
events land relative to other units is a race: the log is no longer deterministic, only
the read models are (for confluent projections). Results that finish together are
committed in claim order. A pool worker that dies takes its in-flight attempts to
``failed`` — never silently re-run — and the host decides, through ``retry``, as for any
failure (spec §11.2: no automatic retry).
"""

from __future__ import annotations

import itertools
import multiprocessing
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, BrokenExecutor, Future, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any

from dizzy_engine import Engine
from dizzy_engine.canonical import canonical_json
from dizzy_engine.engine import _id
from dizzy_engine.store import now
from dizzy_kit.domain import Domain

from dizzy_topology_engine.placement import Lane, Placement, PlacementError
from dizzy_topology_engine.pool import Job, Outcome, init_worker, run_job

_RUNNING = (
    "SELECT c.id, c.type, c.payload, c.cause_event, c.cause_policy, a.id, a.attempt_no"
    " FROM attempts a JOIN commands c ON c.id = a.command_id WHERE a.status = 'running'"
    " ORDER BY c.seq"
)


class TopologyEngine(Engine):
    """The Simple Engine over one data directory, serving a placement's lanes."""

    def __init__(
        self,
        data_dir: str | Path,
        domains: Sequence[Domain],
        placement: Placement | None = None,
        *,
        pool_domains: Callable[[], Sequence[Domain]] | None = None,
        commit: Callable[[], None] | None = None,
        telemetry: bool = True,
        crash_at: int | None = None,
    ):
        """*pool_domains* builds the tables a pool worker runs. It must be a module-level
        function (it is pickled by reference into each spawned worker) and is required
        when the placement declares a pool lane."""
        self.placement = placement or Placement.inline()
        self.pool_domains = pool_domains
        if pool_domains is None and any(
            lane.executor == "pool" for lane in self.placement.lanes.values()
        ):
            raise PlacementError("the placement declares a pool lane but no pool_domains")
        self._pools: dict[str, ProcessPoolExecutor] = {}
        self._inflight: dict[Future, tuple[int, Lane, tuple]] = {}
        self._order = itertools.count()
        super().__init__(data_dir, domains, commit=commit, telemetry=telemetry, crash_at=crash_at)
        unknown = sorted(set(self.placement.commands) - set(self.owner))
        if unknown:
            self.close()
            raise PlacementError(
                f"the placement names commands no domain handles: {unknown} — "
                "was it resolved from a different feature-file?"
            )

    # ── host API ─────────────────────────────────────────────────────────────

    def run(self) -> int:
        """Run units until the queue is empty and no pool lane holds work. Returns how
        many ran."""
        units = 0
        try:
            while True:
                units += self._collect(block=False)
                claimed = self._claim_next()
                if claimed is not None:
                    lane, job = claimed
                    if lane.executor == "inline":
                        self._unit(*job)
                        units += 1
                    else:
                        self._dispatch(lane, job)
                    continue
                if not self._inflight:
                    break
                units += self._collect(block=True)
        except BaseException:
            # In-flight attempts stay 'running' in the store; the next open re-runs them.
            self._shutdown_pools()
            raise
        self.trace.flush()
        return units

    def close(self) -> None:
        self._shutdown_pools()
        super().close()

    # ── lanes ────────────────────────────────────────────────────────────────

    def _has_room(self, lane: Lane) -> bool:
        if lane.executor == "inline":
            return True  # served synchronously: nothing of an inline lane is ever in flight
        busy = sum(1 for _, held, _ in self._inflight.values() if held.name == lane.name)
        return busy < lane.workers

    def _claim_next(self) -> tuple[Lane, tuple] | None:
        """The next unit to run and its lane, or None when nothing with room is waiting."""
        placement = self.placement
        in_flight = {job[6] for _, _, job in self._inflight.values()}

        # An interrupted attempt is still 'running'; it re-runs first, under its own id.
        for name, store in self.stores.items():
            for row in store.all(_RUNNING):
                lane = placement.lane_of(row[1])
                if row[5] not in in_flight and self._has_room(lane):
                    return lane, (name, *row)

        open_lanes = {n for n, lane in placement.lanes.items() if self._has_room(lane)}
        if not open_lanes:
            return None
        where, args = "status = 'queued'", []
        if open_lanes != set(placement.lanes):
            # Only the commands a lane with room serves. (Never `NOT IN (NULL)`: in SQL
            # that matches nothing, so an empty list is spelled out as a constant.)
            listed = list(placement.commands)
            served = [c for c, (lane, _) in placement.commands.items() if lane in open_lanes]
            clauses = []
            if served:
                clauses.append(f"type IN ({_marks(served)})")
                args += served
            if placement.default_lane in open_lanes:
                clauses.append(f"type NOT IN ({_marks(listed)})" if listed else "1")
                args += listed
            where += f" AND ({' OR '.join(clauses) or '0'})"
        interactive = [c for c, (_, p) in placement.commands.items() if p == "interactive"]
        sql = (
            f"SELECT CASE WHEN type IN ({_marks(interactive)}) THEN 0 ELSE 1 END AS rank,"
            " seq, id, type, payload, cause_event, cause_policy FROM commands"
            f" WHERE {where} ORDER BY rank, seq LIMIT 1"
        )
        heads = [
            (row[0], row[1], name, row[2:])
            for name, store in self.stores.items()
            if (row := store.one(sql, *interactive, *args))
        ]
        if not heads:
            return None
        _, _, name, (command_id, ctype, payload, cause_event, cause_policy) = min(heads)

        # The Simple Engine's claim, verbatim: a new attempt, durably, before anything runs.
        store = self.stores[name]
        n = 1 + store.scalar("SELECT count(*) FROM attempts WHERE command_id = ?", command_id)
        attempt_id = _id({"v": 1, "command": command_id, "n": n})
        with store.tx() as db:
            db.execute(
                "INSERT INTO attempts (id, command_id, attempt_no, status, started_at)"
                " VALUES (?, ?, ?, 'running', ?)",
                (attempt_id, command_id, n, now()),
            )
            db.execute("UPDATE commands SET status = 'running' WHERE id = ?", (command_id,))
        self._point()
        job = (name, command_id, ctype, payload, cause_event, cause_policy, attempt_id, n)
        return placement.lane_of(ctype), job

    def _dispatch(self, lane: Lane, job: tuple) -> None:
        pool = self._pools.get(lane.name)
        if pool is None:
            # Built on the lane's first claim, so a run that never reaches a pool spawns
            # nothing at all.
            pool = self._pools[lane.name] = ProcessPoolExecutor(
                max_workers=lane.workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=init_worker,
                initargs=(self.pool_domains,),
            )
        domain, command_id, ctype, payload, _, _, attempt_id, n = job
        future = pool.submit(run_job, Job(domain, command_id, ctype, payload, attempt_id, n))
        self._inflight[future] = (next(self._order), lane, job)

    def _collect(self, block: bool) -> int:
        """Commit every finished pool result, in claim order. Returns how many."""
        if not self._inflight:
            return 0
        if block:
            done = wait(self._inflight, return_when=FIRST_COMPLETED).done
        else:
            done = {f for f in self._inflight if f.done()}
        for future in sorted(done, key=lambda f: self._inflight[f][0]):
            _, lane, job = self._inflight.pop(future)
            try:
                outcome = future.result()
            except BrokenExecutor as exc:
                broken = self._pools.pop(lane.name, None)
                if broken is not None:
                    broken.shutdown(wait=False, cancel_futures=True)
                outcome = Outcome("failed", None, f"{type(exc).__name__}: {exc}", [], 0.0)
            self._commit(lane, job, outcome)
        return len(done)

    def _commit(self, lane: Lane, job: tuple, outcome: Outcome) -> None:
        """The second half of the Simple Engine's unit, for a procedure a child ran:
        append and ack in one transaction, then settle each event."""
        domain, command_id, ctype, _, cause_event, cause_policy, attempt_id, n = job
        store = self.stores[domain]
        status, reason, error = outcome.status, outcome.reason, outcome.error
        ids = {
            "dizzy.domain": domain,
            "dizzy.command_id": command_id,
            "dizzy.attempt_id": attempt_id,
            "dizzy.attempt_no": n,
        }
        unit_attrs: dict[str, Any] = {
            "dizzy.kind": "unit",
            "dizzy.element": ctype,
            "dizzy.lane": lane.name,
            "dizzy.procedure_ms": round(outcome.procedure_ms, 3),
        }
        if cause_event is not None:
            unit_attrs |= {"dizzy.cause_event": cause_event, "dizzy.cause_policy": cause_policy}
        with self.trace.span(
            "dizzy.unit",
            self.trace.span_id("unit", attempt_id),
            self._cause_span(cause_event, cause_policy),
            **ids,
            **unit_attrs,
        ) as unit:
            rows = []
            try:
                for i, (name, body) in enumerate(outcome.emitted):
                    event_id = _id(
                        {"v": 1, "attempt": attempt_id, "i": i, "type": name, "payload": body}
                    )
                    rows.append((event_id, name, canonical_json(body)))
            except Exception as exc:
                status, error, rows = "failed", f"{type(exc).__name__}: {exc}", []
            unit["dizzy.outcome"] = {"done": "ok", "declined": "declined"}.get(status, "error")
            if error is not None:
                unit["dizzy.error"] = error
            with self.trace.span(
                "dizzy.append",
                self.trace.span_id("append", attempt_id),
                **ids,
                **{"dizzy.kind": "append", "dizzy.emitted": len(rows)},
            ):
                appended = []
                with store.tx() as db:
                    for event_id, name, body in rows:
                        cur = db.execute(
                            "INSERT INTO events (id, type, payload, command_id, attempt_id,"
                            " recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (event_id, name, body, command_id, attempt_id, now()),
                        )
                        appended.append(
                            (cur.lastrowid, event_id, name, body, command_id, attempt_id)
                        )
                    db.execute(
                        "UPDATE attempts SET status = ?, reason = ?, error = ?, finished_at = ?"
                        " WHERE id = ?",
                        (status, reason, error, now(), attempt_id),
                    )
                    db.execute("UPDATE commands SET status = ? WHERE id = ?", (status, command_id))
            self._point()
            for row in appended:
                self._settle(domain, row)

    def _shutdown_pools(self) -> None:
        for pool in self._pools.values():
            pool.shutdown(wait=True, cancel_futures=True)
        self._pools.clear()
        self._inflight.clear()


def _marks(items: Sequence[Any]) -> str:
    return ", ".join("?" * len(items)) if items else "NULL"
