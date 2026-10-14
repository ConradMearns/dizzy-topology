"""A feature drawn from spec/specs/graph.qnt's universe, built as real kit Domain tables.

The spec abstracts a feature to a graph — reads, emits, folds, reacts, dispatches,
interactive — and gives every element the same semantics, which this module implements
for real so the topology engine can be run on ANY graph and its store checked against the
spec's invariants (``_spec.py``):

- a procedure emits one event per kind it emits, in KIND order, each carrying a unique
  ``tag`` and the VIEW it read: the tags folded into each model it reads;
- a policy dispatches one command per procedure it dispatches, carrying the view IT read;
- a projection folds an event's tag into its model.

So a model's contents are a set of event tags (the spec uses log positions; a tag maps to
exactly one position through the store). Elements are deterministic in (command, view),
as the spec requires and as at-least-once delivery requires: the tag is derived from the
attempt id, which a re-run keeps.

Module-level so a spawned pool worker can import it (``pool_domains`` is a
``functools.partial`` over a module function and the graph, which pickles by reference).
Run as a script, it is the crash child: ``python _graph_app.py SCENARIO_JSON``.
"""

from __future__ import annotations

import functools
import json
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dizzy_kit.domain import Domain
from pydantic import BaseModel, create_model


@dataclass(frozen=True)
class Graph:
    procs: list[str]
    policies: list[str]
    models: list[str]
    kinds: list[str]
    reads: dict[str, list[str]]  # procs and policies -> models
    emits: dict[str, list[str]]  # procs -> kinds, in KIND order
    folds: dict[str, list[str]]  # models -> kinds
    reacts: dict[str, list[str]]  # policies -> kinds
    dispatches: dict[str, list[str]]  # policies -> procs, in PROC order
    interactive: list[str]

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, text: str) -> Graph:
        return cls(**json.loads(text))


def _small_subset(rng: random.Random, universe: list[str], max_degree: int = 2) -> list[str]:
    """graph.qnt's smallSubsets: out-degree ≤ 2, kept in universe order."""
    picked = set(rng.sample(universe, rng.randint(0, min(max_degree, len(universe)))))
    return [x for x in universe if x in picked]


def random_graph(rng: random.Random, procs=4, policies=2, models=3, kinds=3) -> Graph:
    """A graph whose dispatch relation is acyclic. The spec bounds cascades by log length;
    a real run has no bound, so a cyclic feature would never go idle."""
    P = [f"p{i}" for i in range(1, procs + 1)]
    Y = [f"y{i}" for i in range(1, policies + 1)]
    M = [f"m{i}" for i in range(1, models + 1)]
    K = [f"e{i}" for i in range(1, kinds + 1)]
    while True:
        graph = Graph(
            procs=P,
            policies=Y,
            models=M,
            kinds=K,
            reads={e: _small_subset(rng, M) for e in P + Y},
            emits={p: _small_subset(rng, K) for p in P},
            folds={m: _small_subset(rng, K) for m in M},
            reacts={y: _small_subset(rng, K) for y in Y},
            dispatches={y: _small_subset(rng, P) for y in Y},
            interactive=[p for p in P if rng.random() < 0.3],
        )
        if _acyclic(graph):
            return graph


def _acyclic(g: Graph) -> bool:
    edges = {
        p: {q for y in g.policies if set(g.reacts[y]) & set(g.emits[p]) for q in g.dispatches[y]}
        for p in g.procs
    }
    state: dict[str, int] = {}

    def visit(p: str) -> bool:
        if state.get(p) == 1:
            return False
        if state.get(p) == 2:
            return True
        state[p] = 1
        ok = all(visit(q) for q in edges[p])
        state[p] = 2
        return ok

    return all(visit(p) for p in g.procs)


View = dict[str, list[str]]


def build_domain(graph: Graph, model: dict[str, set[str]] | None = None) -> Domain:
    """The graph as Domain tables, over *model* (model name -> folded tags)."""
    model = {m: set() for m in graph.models} if model is None else model
    commands = {p: create_model(_cls(p), __base__=BaseModel, view=(View, {})) for p in graph.procs}
    events = {
        k: create_model(
            _cls(k), __base__=BaseModel, tag=(str, ...), view=(View, {}), dispatch_view=(View, {})
        )
        for k in graph.kinds
    }
    domain = Domain(name="graph", commands=commands, events=events)

    def view_of(elem: str) -> View:
        return {m: sorted(model[m]) for m in graph.reads[elem]}

    for p in graph.procs:

        def run(command: Any, call: Any, p: str = p) -> None:
            view = view_of(p)
            for i, k in enumerate(graph.emits[p]):
                tag = f"{call.attempt.attempt_id}:{i}"
                call.emitter(k)(events[k](tag=tag, view=view, dispatch_view=command.view))

        domain.procedure(p, p, graph.emits[p], run)

    for y in graph.policies:
        for k in graph.reacts[y]:

            def react(event: Any, call: Any, y: str = y) -> None:
                view = view_of(y)
                for q in graph.dispatches[y]:
                    call.emitter(q)(commands[q](view=view))

            domain.policy(y, k, graph.dispatches[y], react)

    for m in graph.models:
        for k in graph.folds[m]:
            domain.projection(f"{m}_{k}", k, lambda event, m=m: model[m].add(event.tag))

    return domain


def _cls(name: str) -> str:
    return name.upper()


def _pool_domains(graph_json: str) -> list[Domain]:
    return [build_domain(Graph.loads(graph_json))]


def pool_domains_for(graph: Graph) -> functools.partial:
    """A picklable ``pool_domains``: pool procedures are query-free (rule 3), so the
    worker's model is never read."""
    return functools.partial(_pool_domains, graph.dumps())


# --- scenarios ----------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    graph: Graph
    placement: dict  # a placement document, as `dizzy-topology place` writes it
    submissions: list[str]  # host commands, by procedure

    def dumps(self) -> str:
        return json.dumps(
            {
                "graph": asdict(self.graph),
                "placement": self.placement,
                "submissions": self.submissions,
            }
        )

    @classmethod
    def loads(cls, text: str) -> Scenario:
        raw = json.loads(text)
        return cls(Graph(**raw["graph"]), raw["placement"], raw["submissions"])


def random_scenario(rng: random.Random, pools: bool) -> Scenario:
    graph = random_graph(rng)
    # Topology rule 3: only a procedure that reads nothing may be pooled.
    eligible = [p for p in graph.procs if not graph.reads[p]]
    pooled = [p for p in eligible if pools and rng.random() < 0.7]
    lanes = {"main": {"executor": "inline", "workers": 1}}
    if pooled:
        lanes["pool"] = {"executor": "pool", "workers": rng.randint(1, 3)}
    commands = {
        p: {
            "lane": "pool" if p in pooled else "main",
            "priority": "interactive" if p in graph.interactive else "batch",
        }
        for p in graph.procs
    }
    placement = {"format": 1, "lanes": lanes, "default_lane": "main", "commands": commands}
    submissions = [rng.choice(graph.procs) for _ in range(rng.randint(1, 4))]
    return Scenario(graph, placement, submissions)


class Crash(BaseException):
    """An in-process crash: raised at a crash point, a BaseException so no ``except
    Exception`` in the engine can swallow it."""


def _crashing_engine(after: int):
    from dizzy_topology_engine import TopologyEngine

    class CrashingEngine(TopologyEngine):
        """Dies at the *after*-th claim, append or enqueue — the points ``crash_at`` uses,
        each just after a transaction committed, so the store is left exactly as an
        ``os._exit`` there would leave it. Memory is dropped by never reusing the object."""

        def _point(self) -> None:
            self._points += 1
            if self._points == after:
                raise Crash

    return CrashingEngine


def drive(
    data: Path, scenario: Scenario, crash_at: int | None = None, crash_after: int | None = None
):
    """Open the engine over *data* with a fresh, empty read model; submit the scenario's
    host commands by explicit id (idempotent across a reopen); run to idle.

    *crash_at* is the engine's own (``os._exit``, for a child process); *crash_after*
    raises :class:`Crash` in-process at the same points."""
    from dizzy_topology_engine import Placement, TopologyEngine

    model: dict[str, set[str]] = {m: set() for m in scenario.graph.models}
    domain = build_domain(scenario.graph, model)
    cls = TopologyEngine if crash_after is None else _crashing_engine(crash_after)
    engine = cls(
        data,
        [domain],
        Placement.parse(scenario.placement),
        pool_domains=pool_domains_for(scenario.graph),
        telemetry=False,
        crash_at=crash_at,
    )
    for n, p in enumerate(scenario.submissions):
        engine.submit(domain.commands[p](), id=f"host/{n}")
    engine.run()
    points = engine._points
    engine.close()
    return model, points


if __name__ == "__main__":
    scenario = Scenario.loads(Path(sys.argv[1]).read_text())
    drive(Path(sys.argv[2]), scenario, crash_at=int(sys.argv[3]))
    sys.exit("the crash point was never reached")
