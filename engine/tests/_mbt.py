"""Model-based testing: spec/specs/engine.qnt drives the real TopologyEngine, step by step.

Quint generates traces of the spec (``quint run --mbt --out-itf``). Each state names the
action that produced it and the nondeterministic choices that action made. :class:`Driver`
replays every action on a real engine and, after each one, compares the engine's state
with the trace's next state. The comparison is one abstraction function, :meth:`abstract`:
the engine's store (log, command table, cursor) and its process memory (read model, how
far it has folded, the unit in hand, the pool jobs in flight), in the spec's vocabulary.
A difference fails at the step where it first appears, naming the variables that differ.

**How the engine is held at a step.** It runs on its own thread, in a subclass that adds
pauses and changes nothing else. It pauses:

- after each durable transaction: claim, append+ack, each enqueue, the cursor;
- after each event's fold (the ``commit`` hook the Simple Engine calls before policies);
- when ``run()`` returns.

The driver resumes it to the next pause, which must be the one the spec's action names.
First it sets up the environment so the engine's own deterministic choice is the spec's.
It decides which pool job finishes (a :class:`ManualPool` it completes by hand) and which
element raises (``fail``). Host calls (``submit``, ``retry``) run on the engine's thread at
a pause, as a host's run between runs (the spec only allows them when ``hostIdle``). A
crash raises at the pause. The engine is then reopened over the same directory, with a
fresh read model the host seeds, as a real host would.

Several domains: the spec's one log is the host thread's append order across the
stores, which no store records. The engine here is a ``RecordingEngine``, which logs that
order beside the stores as it appends (``_graph_app.ORDER``), and ``_spec.Store.read``
interleaves the stores by it. The spec's one cursor is compared as each store's cursor.

Not compared: ``pending`` (the commands a settle has dispatched and not yet enqueued; the
engine holds them in a local variable, and the enqueue steps that follow show each one),
and the ghost counters.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future
from pathlib import Path
from typing import Any

from _graph_app import (
    Graph,
    RecordingEngine,
    build_domains,
    fresh_model,
    pool_domains_for,
    seed_tag,
)
from _spec import Store

from dizzy_topology_engine import Placement
from dizzy_topology_engine import pool as pool_module

TIMEOUT = 20.0


class Crash(BaseException):
    """Raised in the engine thread at a pause: the process dies there."""


class Mismatch(AssertionError):
    pass


# ── ITF ──────────────────────────────────────────────────────────────────────


def decode(value: Any) -> Any:
    """An ITF value as plain Python: ints, frozensets, dicts, tuples, lists, and
    ``(tag, value)`` for a variant."""
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "#bigint" in value:
        return int(value["#bigint"])
    if "#set" in value:
        return frozenset(_hashable(decode(v)) for v in value["#set"])
    if "#map" in value:
        return {_hashable(decode(k)): decode(v) for k, v in value["#map"]}
    if "#tup" in value:
        return tuple(decode(v) for v in value["#tup"])
    if set(value) == {"tag", "value"}:
        return (value["tag"], decode(value["value"]))
    return {k: decode(v) for k, v in value.items() if not k.startswith("#")}


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    return value


def load_trace(path: Path) -> list[dict]:
    """A trace's states, variables by their bare names (an instance qualifies them:
    ``engine::engineSpec::log``)."""
    return [
        {_bare(k): decode(v) for k, v in state.items()}
        for state in json.loads(path.read_text())["states"]
    ]


def _bare(name: str) -> str:
    return name if name.startswith("mbt::") else name.rsplit("::", 1)[-1]


def pick(state: dict, name: str) -> Any:
    tag, value = state["mbt::nondetPicks"][name]
    assert tag == "Some", (name, state["mbt::actionTaken"])
    return value


# ── the spec's state, normalised ─────────────────────────────────────────────


def _cmd_id(c: dict) -> tuple:
    return (c["origin"], c["at"], c["target"])


def _view(v: dict) -> dict[str, frozenset[int]]:
    return {m: frozenset(s) for m, s in v.items()}


def spec_state(state: dict) -> dict:
    """The variables the driver compares, from one ITF state. The one cursor becomes each
    store's: how many of that store's events lie before it."""
    domain_of = state["g"]["domainOf"]
    stores = [domain_of[e["cause"]["target"]] for e in state["log"]]
    return {
        "log": [
            {
                "kind": e["kind"],
                "cause": _cmd_id(e["cause"]),
                "at": e["at"],
                "view": _view(e["view"]),
                "dispatchView": _view(e["dispatchView"]),
            }
            for e in state["log"]
        ],
        "table": [
            (_cmd_id(r["cmd"]["id"]), r["cmd"]["proc"], _view(r["cmd"]["view"]), r["status"])
            for r in state["table"]
        ],
        "cursors": {d: stores[: state["cursor"]].count(d) for d in _domains(state)},
        "models": _view(state["models"]),
        "foldSeq": {m: list(seq) for m, seq in state["foldSeq"].items()},
        "folded": state["folded"],
        "current": list(state["current"]),
        "inflight": frozenset(state["inflight"]),
    }


def _domains(state: dict) -> list[str]:
    """graph.qnt's DOMAIN_LIST: the core, d1, first."""
    return sorted(set(state["g"]["domainOf"].values()) | {"d1"})


def graph_of(state: dict) -> tuple[Graph, set[str]]:
    """The feature graph and pooled set ``init`` drew, as ``_graph_app`` builds them.
    Relations are lists in universe order: graph.qnt emits in KIND_LIST order and
    dispatches in PROC_LIST order."""
    g = state["g"]
    procs = sorted(g["emits"])
    policies = sorted(g["reacts"])
    models = sorted(g["folds"])
    relations = [*g["emits"].values(), *g["folds"].values(), *g["reacts"].values()]
    kinds = sorted({k for ks in relations for k in ks})
    return (
        Graph(
            procs=procs,
            policies=policies,
            models=models,
            kinds=kinds,
            reads={e: sorted(g["reads"][e]) for e in procs + policies},
            emits={p: sorted(g["emits"][p]) for p in procs},
            folds={m: sorted(g["folds"][m]) for m in models},
            reacts={y: sorted(g["reacts"][y]) for y in policies},
            dispatches={y: sorted(g["dispatches"][y]) for y in policies},
            interactive=sorted(g["interactive"]),
            read_only=sorted(g["readOnly"]),
            domains=_domains(state),
            domain_of=dict(g["domainOf"]),
        ),
        set(state["pooled"]),
    )


# ── the engine, held at its transactions ─────────────────────────────────────


class ManualPool(Executor):
    """A pool lane whose jobs finish when the driver says, in-process."""

    def __init__(self, driver: Driver):
        self.driver = driver

    def submit(self, fn: Callable, /, *args: Any, **kwargs: Any) -> Future:
        (job,) = args
        future: Future = Future()
        self.driver._pool_jobs[job.command_id] = (future, job)
        self.driver._maybe_finish(job.command_id)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        pass


class DrivenEngine(RecordingEngine):
    """The topology engine with pauses. Every override calls the inherited method and
    only records where the engine is; none changes what it does."""

    def __init__(self, driver: Driver, *args: Any, **kwargs: Any):
        self.driver = driver
        self._stack: list[str] = []
        self._held: tuple | None = None  # (lane, job): claimed, not yet run or dispatched
        self._folded = 0
        driver.engine = self
        super().__init__(*args, commit=self._on_commit, telemetry=False, **kwargs)

    def _pause(self, label: str) -> None:
        self.driver._pause(label)

    def _on_commit(self) -> None:
        # The Simple Engine commits the read model after each event's fold (in _settle),
        # and after _recover's refold of each store: one pause once every store is
        # refolded (in between, only memory has changed, and a crash loses memory).
        if self._stack[-1:] == ["settle"]:
            self._pause("fold")
            return
        self._refolded = getattr(self, "_refolded", 0) + 1
        if self._refolded == len(self.stores):
            self._pause("refold")

    def _fold(self, domain: str, row: tuple) -> Any:
        event = super()._fold(domain, row)
        self._folded += 1  # events folded since open, across every store
        return event

    def _claim_next(self):
        self._stack.append("claim")
        try:
            claimed = super()._claim_next()
        finally:
            self._stack.pop()
        if claimed is not None:
            self._held = claimed
            self._pause("claim")
        return claimed

    def _dispatch(self, lane, job) -> None:
        self._held = None
        super()._dispatch(lane, job)

    def _unit(self, *job) -> None:
        self._stack.append("append")
        try:
            super()._unit(*job)
        finally:
            self._stack.pop()

    def _commit(self, lane, job, outcome) -> None:
        self._stack.append("append")
        try:
            super()._commit(lane, job, outcome)
        finally:
            self._stack.pop()

    def _settle(self, domain: str, row: tuple) -> None:
        self._stack.append("settle")
        try:
            super()._settle(domain, row)
        finally:
            self._stack.pop()
        self._pause("cursor")

    def _enqueue(self, *args: Any, **kwargs: Any) -> None:
        self._stack.append("enqueue")
        try:
            super()._enqueue(*args, **kwargs)
        finally:
            self._stack.pop()

    def _point(self) -> None:
        super()._point()
        kind = self._stack[-1] if self._stack else None
        if kind == "append":
            self._held = None  # an inline unit's append ends its time "in hand"
            self._pause("append")
        elif kind == "enqueue":
            self._pause("enqueue")

    def _new_pool(self, lane) -> Executor:
        return ManualPool(self.driver)


# Which pause each spec action ends at.
_ENDS_AT = {
    "claim": "claim",
    "runInline": "append",
    "failInline": "append",
    "completePool": "append",
    "failPool": "append",
    "settleFold": "fold",
    "enqueue": "enqueue",
    "advance": "cursor",
}


class Driver:
    """One real engine over *data*, replaying one spec trace of an instance whose pool
    lane is *pool_workers* wide."""

    def __init__(self, data: Path, graph: Graph, pooled: set[str], pool_workers: int):
        self.data = data
        self.graph = graph
        commands = {
            p: {
                "lane": "pool" if p in pooled else "main",
                "priority": "interactive" if p in graph.interactive else "batch",
            }
            for p in graph.procs
        }
        self.placement = Placement.parse(
            {
                "format": 1,
                "lanes": {
                    "main": {"executor": "inline", "workers": 1},
                    "pool": {"executor": "pool", "workers": pool_workers},
                },
                "default_lane": "main",
                "commands": commands,
            }
        )
        self.fail: set[str] = set()  # command ids whose next run raises
        self._finish: set[str] = set()  # pool jobs the driver has let finish
        self._pool_jobs: dict[str, tuple[Future, Any]] = {}
        self.submitted = 0
        # The in-process stand-in for a pool worker's initializer: its own tables, over
        # its own model (a seeded copy of each read-only model, nothing else).
        pool_module._TABLES.clear()
        pool_module.init_worker(lambda: build_domains(graph, fresh_model(graph), fail=self.fail))
        self._open()

    # ── the engine thread ──

    def _open(self) -> None:
        self.engine: DrivenEngine | None = None
        self.model = fresh_model(self.graph)
        self.domains = build_domains(self.graph, self.model, fail=self.fail)
        self.commands = {k: v for d in self.domains for k, v in d.commands.items()}
        self._out: queue.Queue = queue.Queue()
        self._in: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()
        self._expect("refold")  # the constructor's _recover

    def _main(self) -> None:
        try:
            engine = DrivenEngine(
                self,
                self.data,
                self.domains,
                self.placement,
                pool_domains=pool_domains_for(self.graph),
            )
            while True:
                engine.run()
                engine._pause("idle")
        except Crash:
            self._out.put("crashed")
        except BaseException as exc:  # noqa: BLE001 — reported to the driver thread
            self._out.put(exc)
        finally:
            if self.engine is not None:
                for store in self.engine.stores.values():
                    store.close()

    def _pause(self, label: str) -> None:
        """Called on the engine thread: report *label*, then do what the driver says."""
        while True:
            self._out.put(label)
            order = self._in.get()
            if order == "go":
                return
            if order == "crash":
                raise Crash
            order(self.engine)  # a host call, on the engine's thread
            label = "called"

    # ── the driver thread ──

    def _next(self) -> str:
        try:
            got = self._out.get(timeout=TIMEOUT)
        except queue.Empty:
            raise Mismatch("the engine did not reach a pause (blocked?)") from None
        if isinstance(got, BaseException):
            raise got
        return got

    def _expect(self, label: str) -> None:
        got = self._next()
        if got != label:
            raise Mismatch(f"the engine paused at '{got}', the spec's step ends at '{label}'")

    def _go(self, label: str) -> None:
        self._in.put("go")
        self._expect(label)

    def _call(self, fn: Callable[[DrivenEngine], Any]) -> None:
        self._in.put(fn)
        while self._next() != "called":
            self._in.put("go")  # a transaction inside the host call (submit's enqueue)

    def _maybe_finish(self, command_id: str) -> None:
        if command_id in self._finish and command_id in self._pool_jobs:
            future, job = self._pool_jobs.pop(command_id)
            self._finish.discard(command_id)
            future.set_result(pool_module.run_job(job))

    def crash(self) -> None:
        self._in.put("crash")
        while self._next() != "crashed":
            pass
        self._thread.join(TIMEOUT)
        self._pool_jobs.clear()  # the old engine's futures died with it

    def close(self) -> None:
        if self._thread.is_alive():
            try:
                self.crash()
            except Mismatch:
                pass  # already failing; the daemon thread is abandoned

    # ── one spec step ──

    def step(self, state: dict) -> None:
        """Do what *state*'s action did, on the engine, and stop where it ends."""
        action = state["mbt::actionTaken"]
        ids = self._command_ids()
        if action in ("init", "initCrossStore", "quiescent"):
            return
        if action == "submit":
            p, n = pick(state, "p"), self.submitted
            self._call(lambda e: e.submit(self.commands[p](), id=f"host/{n}"))
            self.submitted += 1
        elif action == "retry":
            command_id = ids[pick(state, "i")]
            self._call(lambda e: e.retry(command_id))
        elif action == "crash":
            self.crash()
            self._open()
        elif action in ("failInline", "runInline"):
            if self.engine._held is None:
                raise Mismatch("the spec runs an inline unit the engine does not hold")
            _, job = self.engine._held
            command_id = job[1]
            if action == "failInline":
                self.fail.add(command_id)
            self._go("append")
            self.fail.discard(command_id)
        elif action in ("completePool", "failPool"):
            command_id = ids[pick(state, "j")]
            if action == "failPool":
                self.fail.add(command_id)
            self._finish.add(command_id)
            self._maybe_finish(command_id)
            self._go("append")
            self.fail.discard(command_id)
        else:
            self._go(_ENDS_AT[action])

    def _command_ids(self) -> list[str]:
        return [c.id for c in sorted(Store.read(self.data).commands.values(), key=lambda c: c.seq)]

    # ── the abstraction function ──

    def abstract(self) -> dict:
        """The engine's store and memory as engine.qnt's variables."""
        g, engine = self.graph, self.engine
        store = Store.read(self.data)
        pos_of_tag = {e.tag: e.pos for e in store.log}
        pos_of_id = {e.id: e.pos for e in store.log}
        ordered = sorted(store.commands.values(), key=lambda c: c.seq)
        index = {c.id: i for i, c in enumerate(ordered)}
        block_start: dict[str, int] = {}
        for e in store.log:
            block_start.setdefault(e.attempt_id, e.pos)

        def position(m: str, tag: str) -> int:
            return -1 if tag == seed_tag(m) else pos_of_tag.get(tag, -2)

        def view(v: dict) -> dict[str, frozenset[int]]:
            return {m: frozenset(position(m, t) for t in v.get(m, [])) for m in g.models}

        def cmd_id(c) -> tuple:
            if c.origin == "host":
                return ("host", int(c.id.split("/")[1]), c.proc)
            return (c.cause_policy, pos_of_id[c.cause_event], c.proc)

        current: list[int] = []
        inflight = {index[job[1]] for _, _, job in engine._inflight.values()}
        if engine._held is not None:
            lane, job = engine._held
            if lane.executor == "inline":
                current.append(index[job[1]])
            else:
                inflight.add(index[job[1]])
        return {
            "log": [
                {
                    "kind": e.kind,
                    "cause": cmd_id(store.commands[e.command_id]),
                    "at": block_start[e.attempt_id],
                    "view": view(e.view),
                    "dispatchView": view(e.dispatch_view),
                }
                for e in store.log
            ],
            "table": [(cmd_id(c), c.proc, view(c.view), c.status) for c in ordered],
            "cursors": {d: store.cursors.get(d, 0) for d in g.domains},
            "models": view({m: sorted(set(tags)) for m, tags in self.model.items()}),
            # Fold order: a read-only model is seeded, never folded (the spec's foldSeq is
            # empty for it).
            "foldSeq": {
                m: [] if m in g.read_only else [position(m, t) for t in tags]
                for m, tags in self.model.items()
            },
            "folded": engine._folded,
            "current": current,
            "inflight": frozenset(inflight),
        }


def diff(engine: dict, spec: dict) -> str:
    lines = []
    for key in spec:
        if engine[key] != spec[key]:
            lines.append(f"  {key}:\n    engine {engine[key]}\n    spec   {spec[key]}")
    return "\n".join(lines)


def replay(trace: list[dict], data: Path, pool_workers: int) -> int:
    """Replay *trace* on a real engine over *data*, comparing after every step. Returns
    the number of steps compared."""
    graph, pooled = graph_of(trace[0])
    driver = Driver(data, graph, pooled, pool_workers)
    try:
        for k, state in enumerate(trace):
            action = state["mbt::actionTaken"]
            try:
                driver.step(state)
            except Mismatch as exc:
                raise Mismatch(f"step {k} ({action}): {exc}") from None
            engine, spec = driver.abstract(), spec_state(state)
            if engine != spec:
                done = " → ".join(s["mbt::actionTaken"] for s in trace[: k + 1])
                raise Mismatch(f"step {k} ({action}) after {done}:\n{diff(engine, spec)}")
        return len(trace)
    finally:
        driver.close()
