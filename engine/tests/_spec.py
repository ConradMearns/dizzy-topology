"""spec/specs/engine.qnt's invariants, evaluated on a store the real engine wrote.

Each check is a direct port of the Quint definition it names (graph.qnt's log
predicates, engine.qnt's E-invariants), so a run of the topology engine is held to the
same statements the model checker proves of the design. Two translations, both exact:

- the spec's log positions are the store's ``events.seq - 1``;
- the spec's views are sets of positions; here they are sets of event TAGS, which the
  store maps to positions one to one (``_graph_app``).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from _graph_app import Graph


@dataclass
class Event:
    pos: int
    id: str
    kind: str
    tag: str
    view: dict[str, list[str]]
    dispatch_view: dict[str, list[str]]
    command_id: str
    attempt_id: str


@dataclass
class Command:
    seq: int
    id: str
    proc: str
    view: dict[str, list[str]]
    origin: str
    cause_event: str | None
    cause_policy: str | None
    status: str


@dataclass
class Store:
    log: list[Event]
    commands: dict[str, Command]
    cursor: int

    @classmethod
    def read(cls, data: Path) -> Store:
        db = sqlite3.connect(data / "graph.db")
        log = []
        for seq, eid, kind, payload, cid, aid in db.execute(
            "SELECT seq, id, type, payload, command_id, attempt_id FROM events ORDER BY seq"
        ):
            body = json.loads(payload)
            log.append(
                Event(
                    seq - 1,
                    eid,
                    kind,
                    body["tag"],
                    body.get("view", {}),
                    body.get("dispatch_view", {}),
                    cid,
                    aid,
                )
            )
        commands = {}
        for seq, cid, proc, payload, origin, ce, cp, status in db.execute(
            "SELECT seq, id, type, payload, origin, cause_event, cause_policy, status"
            " FROM commands ORDER BY seq"
        ):
            commands[cid] = Command(
                seq, cid, proc, json.loads(payload).get("view", {}), origin, ce, cp, status
            )
        row = db.execute("SELECT seq FROM cursors WHERE source = 'graph'").fetchone()
        db.close()
        return cls(log, commands, row[0] if row else 0)


def check(g: Graph, store: Store, model: dict[str, set[str]] | None = None) -> list[str]:
    """Every invariant engine.qnt proves, on this store. Returns the violations."""
    log, commands = store.log, store.commands
    errors: list[str] = []
    pos_of_tag = {e.tag: e.pos for e in log}
    pos_of_id = {e.id: e.pos for e in log}
    if len(pos_of_tag) != len(log):
        errors.append("two events share a tag")

    def fold_prefix(upto: int) -> dict[str, set[int]]:
        """graph.qnt foldPrefix: the positions < upto each model folds."""
        return {m: {e.pos for e in log if e.pos < upto and e.kind in g.folds[m]} for m in g.models}

    def positions(view: dict[str, list[str]]) -> dict[str, set[int]]:
        return {m: {pos_of_tag.get(t, -1) for t in tags} for m, tags in view.items()}

    def restrict(elem: str, v: dict[str, set[int]]) -> dict[str, set[int]]:
        return {m: v[m] for m in g.reads[elem]}

    # Blocks: one attempt's appends.
    blocks: dict[str, list[Event]] = defaultdict(list)
    for e in log:
        blocks[e.attempt_id].append(e)
    block_start = {aid: es[0].pos for aid, es in blocks.items()}

    # E3 blocksWellFormed: a complete, contiguous run of its procedure, in KIND order.
    for aid, es in blocks.items():
        proc = commands[es[0].command_id].proc
        if [e.kind for e in es] != g.emits[proc]:
            errors.append(
                f"blocksWellFormed: block {aid[:8]} is {[e.kind for e in es]}, "
                f"{proc} emits {g.emits[proc]}"
            )
        if [e.pos for e in es] != list(range(es[0].pos, es[0].pos + len(es))):
            errors.append(f"blocksWellFormed: block {aid[:8]} is not contiguous")
        if len({e.command_id for e in es}) != 1:
            errors.append(f"blocksWellFormed: block {aid[:8]} mixes causes")

    # E4 effectsOnce: every cause owns exactly one block — unconditionally.
    attempts_of: dict[str, set[str]] = defaultdict(set)
    for e in log:
        attempts_of[e.command_id].add(e.attempt_id)
    for cid, aids in attempts_of.items():
        if len(aids) != 1:
            errors.append(f"effectsOnce: command {cid[:8]} appended {len(aids)} blocks")

    # E5 ackAtomic: a block's command is done; a done command that emits has its block;
    # anything not done appended nothing.
    for cid, c in commands.items():
        has_block = cid in attempts_of
        if c.status == "done" and g.emits[c.proc] and not has_block:
            errors.append(f"ackAtomic: {c.proc} {cid[:8]} is done with no block")
        if c.status != "done" and has_block:
            errors.append(f"ackAtomic: {c.proc} {cid[:8]} is {c.status} but appended")

    # E6 causal: a policy-caused block follows a trigger its policy reacts to, and the
    # policy dispatches its procedure.
    for e in log:
        c = commands[e.command_id]
        if c.origin == "policy":
            at = pos_of_id.get(c.cause_event, -1)
            if not (0 <= at < block_start[e.attempt_id]):
                errors.append(f"causal: {c.proc} block precedes or lacks its trigger")
            elif log[at].kind not in g.reacts[c.cause_policy]:
                errors.append(f"causal: {c.cause_policy} does not react to {log[at].kind}")
            if c.proc not in g.dispatches[c.cause_policy]:
                errors.append(f"causal: {c.cause_policy} does not dispatch {c.proc}")

    # E7 freshReads (rule 3 holds by construction): every procedure read the fold of
    # everything before its block, restricted to what it declares.
    for e in log:
        proc = commands[e.command_id].proc
        expected = restrict(proc, fold_prefix(block_start[e.attempt_id]))
        if positions(e.view) != expected:
            errors.append(
                f"freshReads: {proc} at {e.pos} read {positions(e.view)}, "
                f"the fold before its block is {expected}"
            )

    # E8 freshDispatch / freshQueue: a dispatching policy read exactly the fold through
    # its trigger — checked on every command's payload (queue) and every event (log).
    for c in commands.values():
        if c.origin != "policy" or c.cause_event not in pos_of_id:
            continue
        expected = restrict(c.cause_policy, fold_prefix(pos_of_id[c.cause_event] + 1))
        if positions(c.view) != expected:
            errors.append(
                f"freshQueue: {c.cause_policy}→{c.proc} read {positions(c.view)}, "
                f"the fold through its trigger is {expected}"
            )
    for e in log:
        c = commands[e.command_id]
        if positions(e.dispatch_view) != positions(c.view):
            errors.append(f"freshDispatch: block at {e.pos} carries a view its command lacks")

    # E9 dispatchComplete: every reacting policy of every settled event has its commands.
    dispatched = {(c.cause_event, c.cause_policy, c.proc) for c in commands.values()}
    for e in log:
        if e.pos >= store.cursor:
            continue
        for y in g.policies:
            if e.kind in g.reacts[y]:
                for q in g.dispatches[y]:
                    if (e.id, y, q) not in dispatched:
                        errors.append(f"dispatchComplete: {y} never dispatched {q} for {e.pos}")

    # E1 modelsAreFold, at idle: the read model is the fold of the whole log.
    if model is not None:
        for m in g.models:
            want = {e.tag for e in log if e.kind in g.folds[m]}
            if model[m] != want:
                errors.append(
                    f"modelsAreFold: {m} holds {len(model[m])} tags, the fold {len(want)}"
                )

    return errors


def check_idle(g: Graph, store: Store, model: dict[str, set[str]]) -> list[str]:
    """``check`` plus what an idle engine owes: nothing queued or running, all settled."""
    errors = check(g, store, model)
    if store.cursor != len(store.log):
        errors.append(f"idle with cursor {store.cursor} behind {len(store.log)} events")
    stuck = [c.proc for c in store.commands.values() if c.status != "done"]
    if stuck:
        errors.append(f"idle with commands not done: {stuck}")
    return errors
