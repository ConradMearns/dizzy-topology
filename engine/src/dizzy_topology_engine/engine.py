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
the read models are (for confluent projections). Finished results are committed one per
turn of the loop, earliest claimed first, before the next claim. A pool worker that dies
takes its in-flight attempts to ``failed`` — never silently re-run — and the host
decides, through ``retry``, as for any failure (spec §11.2: no automatic retry).

**Telemetry** is the Simple Engine's trace, with lanes, pooled units timed claim to
settle (the worker's own time inside), pool and worker spans, and each lane's queue as
up/down counters (:mod:`dizzy_topology_engine.telemetry`, ``docs/telemetry.md``). No
decision reads it.
"""

from __future__ import annotations

import itertools
import multiprocessing
from collections.abc import Callable, Sequence
from concurrent.futures import (
    FIRST_COMPLETED,
    BrokenExecutor,
    Executor,
    Future,
    ProcessPoolExecutor,
    wait,
)
from datetime import datetime
from pathlib import Path
from typing import Any

from dizzy_engine import Engine
from dizzy_engine.canonical import canonical_json
from dizzy_engine.engine import _id
from dizzy_engine.store import now
from dizzy_kit.domain import Domain

from dizzy_topology_engine.placement import Lane, Placement, PlacementError
from dizzy_topology_engine.pool import Job, Outcome, init_worker, run_job
from dizzy_topology_engine.telemetry import Clock, Metrics, Telemetry

_RUNNING = (
    "SELECT c.seq, c.id, c.type, c.payload, c.cause_event, c.cause_policy, a.id, a.attempt_no"
    " FROM attempts a JOIN commands c ON c.id = a.command_id WHERE a.status = 'running'"
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
        self._pools: dict[str, Executor] = {}
        self._inflight: dict[Future, tuple[int, Lane, tuple]] = {}
        self._order = itertools.count()
        # Telemetry only (never read by a decision): each claim's moment and unit
        # attributes, by attempt id; each live pool's span; how many pools each lane built.
        self._claims: dict[str, tuple[Clock, dict[str, Any]]] = {}
        self._pool_spans: dict[str, dict[str, Any]] = {}
        self._pools_built: dict[str, int] = {}
        self._metrics: Metrics | None = None  # counted from the stores once they're open
        super().__init__(data_dir, domains, commit=commit, telemetry=telemetry, crash_at=crash_at)
        self.trace = Telemetry(self.trace)
        self.trace.annotate_run(
            **{
                "dizzy.lanes": ", ".join(
                    f"{n}={lane.executor}x{lane.workers}"
                    for n, lane in self.placement.lanes.items()
                ),
                "dizzy.default_lane": self.placement.default_lane,
            }
        )
        self._metrics = self.trace.metrics
        self._recount()
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
                if self._collect(block=False):
                    units += 1
                    continue
                claimed = self._claim_next()
                if claimed is not None:
                    lane, job = claimed
                    if lane.executor == "inline":
                        _, self.trace.unit_attrs = self._claims.pop(job[6])
                        try:
                            self._unit(*job)
                        finally:
                            self.trace.unit_attrs = {}
                        self._count(job, in_flight=-1)
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
            self._recount()
            raise
        self.trace.flush()
        return units

    def retry(self, command_id: str) -> None:
        super().retry(command_id)
        for name, store in self.stores.items():
            ctype = store.scalar("SELECT type FROM commands WHERE id = ?", command_id)
            if ctype is not None:
                self._count((name, command_id, ctype), queued=1)
                return

    def close(self) -> None:
        self._shutdown_pools()
        super().close()

    def _enqueue(
        self,
        name: str,
        command: Any,
        command_id: str,
        origin: str,
        cause_event: str | None = None,
        cause_policy: str | None = None,
    ) -> None:
        """The Simple Engine's enqueue, counted: a known id is not queued again."""
        domain = self.owner[name][0] if name in self.owner else None
        before = self.stores[domain].db.total_changes if domain else 0
        super()._enqueue(name, command, command_id, origin, cause_event, cause_policy)
        if domain and self.stores[domain].db.total_changes > before:
            self._count((domain, command_id, name), queued=1)

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

        # An interrupted attempt is still 'running'; it re-runs first, under its own id, in
        # seq order across every store, as queued work is claimed: with several domains
        # (and pools) more than one attempt can be interrupted, in different stores.
        running = sorted(
            (row[0], name, row[1:])
            for name, store in self.stores.items()
            for row in store.all(_RUNNING)
        )
        for _, name, row in running:
            lane = placement.lane_of(row[1])
            if row[5] not in in_flight and self._has_room(lane):
                return self._claimed(lane, (name, *row), recovered=True)

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
        return self._claimed(placement.lane_of(ctype), job, recovered=False)

    def _claimed(self, lane: Lane, job: tuple, recovered: bool) -> tuple[Lane, tuple]:
        """Note a claim's moment and its unit's lane attributes, for telemetry."""
        name, command_id, ctype, *_ = job
        clock = Clock.now()
        enqueued = self.stores[name].scalar(
            "SELECT enqueued_at FROM commands WHERE id = ?", command_id
        )
        attrs: dict[str, Any] = {
            "dizzy.lane": lane.name,
            "dizzy.executor": lane.executor,
            "dizzy.priority": self.placement.priority_of(ctype),
        }
        if enqueued:
            queued = clock.ns / 1e9 - datetime.fromisoformat(enqueued).timestamp()
            attrs["dizzy.queued_ms"] = round(max(queued, 0.0) * 1000, 3)
        if recovered:
            attrs["dizzy.recovered"] = True
        self._claims[job[6]] = (clock, attrs)
        self._count(job, queued=-1, in_flight=1)
        return lane, job

    def _dispatch(self, lane: Lane, job: tuple) -> None:
        pool = self._pools.get(lane.name)
        if pool is None:
            # Built on the lane's first claim, so a run that never reaches a pool spawns
            # nothing at all.
            pool = self._pools[lane.name] = self._new_pool(lane)
            built = self._pools_built[lane.name] = self._pools_built.get(lane.name, 0) + 1
            self._pool_spans[lane.name] = {
                "id": self.trace.span_id("pool", lane.name, str(built)),
                "clock": Clock.now(),
                "attrs": {
                    "dizzy.kind": "pool",
                    "dizzy.lane": lane.name,
                    "dizzy.pool.workers": lane.workers,
                    "dizzy.pool.jobs": 0,
                    "dizzy.pool.failed": 0,
                },
                "workers": set(),
            }
        domain, command_id, ctype, payload, _, _, attempt_id, n = job
        future = pool.submit(run_job, Job(domain, command_id, ctype, payload, attempt_id, n))
        self._inflight[future] = (next(self._order), lane, job)

    def _new_pool(self, lane: Lane) -> Executor:
        """A pool lane's executor: *lane.workers* spawned processes, each running
        ``pool_domains`` once. The model-based tests replace it with one whose jobs
        finish in the order the spec's trace says (``engine/tests/_mbt.py``)."""
        return ProcessPoolExecutor(
            max_workers=lane.workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=init_worker,
            initargs=(self.pool_domains,),
        )

    def _collect(self, block: bool) -> int:
        """Commit ONE finished pool result, the earliest claimed. Returns how many (0/1).

        One per call, and the loop asks again before it claims: a result that finished
        while another was being committed goes in before the next claim, which is the
        step engine.qnt takes (``completePool`` of any finished job, whenever settled).
        Results that finish together are still committed in claim order."""
        if not self._inflight:
            return 0
        if block:
            done = wait(self._inflight, return_when=FIRST_COMPLETED).done
        else:
            done = {f for f in self._inflight if f.done()}
        if not done:
            return 0
        future = min(done, key=lambda f: self._inflight[f][0])
        _, lane, job = self._inflight.pop(future)
        try:
            outcome = future.result()
        except BrokenExecutor as exc:
            broken = self._pools.pop(lane.name, None)
            if broken is not None:
                broken.shutdown(wait=False, cancel_futures=True)
            outcome = Outcome("failed", None, f"{type(exc).__name__}: {exc}", [], 0.0)
            self._end_pool_span(lane.name, error=outcome.error)
        self._commit(lane, job, outcome)
        self._count(job, in_flight=-1)
        return 1

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
        claimed_at, lane_attrs = self._claims.pop(attempt_id, (Clock.now(), {}))
        unit_attrs: dict[str, Any] = {
            "dizzy.kind": "unit",
            "dizzy.element": ctype,
            **lane_attrs,
            "dizzy.procedure_ms": round(outcome.procedure_ms, 3),
        }
        if cause_event is not None:
            unit_attrs |= {"dizzy.cause_event": cause_event, "dizzy.cause_policy": cause_policy}
        unit_id = self.trace.span_id("unit", attempt_id)
        with self.trace.span_since(
            "dizzy.unit",
            unit_id,
            self._cause_span(cause_event, cause_policy),
            claimed_at,
            **ids,
            **unit_attrs,
        ) as unit:
            self._trace_pool_job(lane, job, outcome, claimed_at, unit_id, ids)
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
        for lane in list(self._pool_spans):
            self._end_pool_span(lane)

    # ── telemetry ────────────────────────────────────────────────────────────

    def _queue_attrs(self, domain: str, ctype: str) -> dict[str, Any]:
        return {
            "dizzy.domain": domain,
            "dizzy.lane": self.placement.lane_of(ctype).name,
            "dizzy.priority": self.placement.priority_of(ctype),
        }

    def _count(self, job: tuple, queued: int = 0, in_flight: int = 0) -> None:
        """Move a command between its lane's queue and its in-flight count."""
        if self._metrics is None:
            return  # still opening: _recount counts what the open left queued
        attrs = self._queue_attrs(job[0], job[2])
        self._metrics.add("dizzy.lane.queued", queued, **attrs)
        self._metrics.add("dizzy.lane.in_flight", in_flight, **attrs)

    def _recount(self) -> None:
        """Set the counters from the stores, where nothing is in flight: on open, and
        when a run is interrupted. Waiting means queued or an interrupted attempt."""
        if self._metrics is None:
            return
        queued: dict[tuple, int] = {}
        for name, store in self.stores.items():
            for ctype, n in store.all(
                "SELECT type, count(*) FROM commands"
                " WHERE status IN ('queued', 'running') GROUP BY type"
            ):
                key = tuple(sorted(self._queue_attrs(name, ctype).items()))
                queued[key] = queued.get(key, 0) + n
        self._metrics.reset("dizzy.lane.queued", queued)
        self._metrics.reset("dizzy.lane.in_flight", {})

    def _trace_pool_job(
        self, lane: Lane, job: tuple, outcome: Outcome, claimed_at: Clock, unit_id: str, ids: dict
    ) -> None:
        """A pooled unit's time away from the host thread, on the worker's clock: queued in
        the pool, the procedure in the worker, and the wait for the host to collect it."""
        pool = self._pool_spans.get(lane.name)
        if pool is not None:
            pool["attrs"]["dizzy.pool.jobs"] += 1
            pool["attrs"]["dizzy.pool.failed"] += outcome.status == "failed"
        if not outcome.pid:
            return  # no worker ran it: the pool broke under it
        attempt_id = job[6]
        on = {**ids, "dizzy.lane": lane.name, "process.pid": outcome.pid}
        if outcome.worker_start is not None and pool is not None:
            start, end = outcome.worker_start
            pool["workers"].add(outcome.pid)
            worker_id = self.trace.span_id("worker", pool["id"], str(outcome.pid))
            self.trace.record(
                "dizzy.worker",
                worker_id,
                pool["id"],
                start,
                end,
                **{"dizzy.kind": "worker", "dizzy.lane": lane.name, "process.pid": outcome.pid},
            )
        self.trace.record(
            "dizzy.pool.wait",
            self.trace.span_id("wait", attempt_id),
            unit_id,
            claimed_at.ns,
            outcome.started_ns,
            **{**on, "dizzy.kind": "wait", "dizzy.pool.cold": outcome.worker_start is not None},
        )
        _, proc = self.owner[job[2]]
        procedure = {**on, "dizzy.kind": "procedure", "dizzy.element": proc.name}
        procedure["dizzy.outcome"] = {"done": "ok", "declined": "declined"}.get(
            outcome.status, "error"
        )
        if outcome.error is not None:
            procedure["dizzy.error"] = outcome.error
        procedure["dizzy.emitted"] = len(outcome.emitted)
        self.trace.record(
            f"dizzy.procedure {proc.name}",
            self.trace.span_id("procedure", attempt_id),
            unit_id,
            outcome.started_ns,
            outcome.finished_ns,
            **procedure,
        )
        self.trace.record(
            "dizzy.pool.handoff",
            self.trace.span_id("handoff", attempt_id),
            unit_id,
            outcome.finished_ns,
            Clock.now().ns,
            **{**on, "dizzy.kind": "handoff"},
        )

    def _end_pool_span(self, lane: str, error: str | None = None) -> None:
        pool = self._pool_spans.pop(lane, None)
        if pool is None:
            return
        attrs = {**pool["attrs"], "dizzy.pool.started_workers": len(pool["workers"])}
        if error is not None:
            attrs |= {"dizzy.outcome": "error", "dizzy.error": error, "dizzy.pool.broken": True}
        end = pool["clock"].ns + int((Clock.now().perf - pool["clock"].perf) * 1e9)
        self.trace.record(
            f"dizzy.pool {lane}",
            pool["id"],
            self.trace.span_id("run"),
            pool["clock"].ns,
            end,
            **attrs,
        )


def _marks(items: Sequence[Any]) -> str:
    return ", ".join("?" * len(items)) if items else "NULL"
