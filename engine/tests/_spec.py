"""spec/specs/engine.qnt's invariants, evaluated on a store the real engine wrote.

Each check is a direct port of the Quint definition it names (graph.qnt's log
predicates, engine.qnt's E-invariants), so a run of the topology engine is held to the
same statements the model checker proves of the design. Two translations, both exact:

- the spec's one log is the stores' chains, interleaved in the order the host thread
  appended them (``_graph_app.global_order``); its positions index that interleaving;
- the spec's views are sets of positions; here they are sets of event TAGS, which the
  store maps to positions one to one (``_graph_app``), and a read-only model's seed tag
  maps to the spec's SEED position, -1.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from _graph_app import Graph, global_order, seed_tag

SEED = -1
UNKNOWN = -2  # a tag no event carries and no seed is: never equal to a real position


@dataclass
class Event:
    pos: int
    domain: str
    seq: int  # in its store
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
    domain: str
    id: str
    proc: str
    view: dict[str, list[str]]
    origin: str
    cause_event: str | None
    cause_policy: str | None
    status: str


@dataclass
class Store:
    """Every domain's store, read together: one log in append order, one command table in
    (shared) seq order, and each store's reaction cursor."""

    log: list[Event]
    commands: dict[str, Command]
    cursors: dict[str, int]

    def settled(self, e: Event) -> bool:
        return e.seq <= self.cursors.get(e.domain, 0)

    @classmethod
    def read(cls, data: Path) -> Store:
        events: dict[tuple[str, int], tuple] = {}
        commands = {}
        cursors = {}
        for path in sorted(data.glob("*.db")):
            domain = path.stem
            db = sqlite3.connect(path)
            for seq, eid, kind, payload, cid, aid in db.execute(
                "SELECT seq, id, type, payload, command_id, attempt_id FROM events"
            ):
                events[(domain, seq)] = (eid, kind, json.loads(payload), cid, aid)
            for seq, cid, proc, payload, origin, ce, cp, status in db.execute(
                "SELECT seq, id, type, payload, origin, cause_event, cause_policy, status"
                " FROM commands"
            ):
                view = json.loads(payload).get("view", {})
                commands[cid] = Command(seq, domain, cid, proc, view, origin, ce, cp, status)
            row = db.execute("SELECT seq FROM cursors WHERE source = ?", (domain,)).fetchone()
            cursors[domain] = row[0] if row else 0
            db.close()
        order = global_order(data)
        if sorted(order) != sorted(events):
            raise AssertionError(f"the append-order record covers {len(order)} of {len(events)}")
        log = []
        for pos, (domain, seq) in enumerate(order):
            eid, kind, body, cid, aid = events[(domain, seq)]
            log.append(
                Event(
                    pos,
                    domain,
                    seq,
                    eid,
                    kind,
                    body["tag"],
                    body.get("view", {}),
                    body.get("dispatch_view", {}),
                    cid,
                    aid,
                )
            )
        commands = dict(sorted(commands.items(), key=lambda kv: kv[1].seq))
        return cls(log, commands, cursors)


def check(g: Graph, store: Store, model: dict[str, set[str]] | None = None) -> list[str]:
    """Every invariant engine.qnt proves, on this store. Returns the violations."""
    log, commands = store.log, store.commands
    errors: list[str] = []
    pos_of_tag = {e.tag: e.pos for e in log}
    pos_of_id = {e.id: e.pos for e in log}
    if len(pos_of_tag) != len(log):
        errors.append("two events share a tag")

    def fold_prefix(upto: int) -> dict[str, set[int]]:
        """graph.qnt foldPrefix: the seed, and the positions < upto each model folds."""
        return {
            m: ({SEED} if m in g.read_only else set())
            | {e.pos for e in log if e.pos < upto and e.kind in g.folds[m]}
            for m in g.models
        }

    def position(m: str, tag: str) -> int:
        return SEED if tag == seed_tag(m) else pos_of_tag.get(tag, UNKNOWN)

    def positions(view: dict[str, list[str]]) -> dict[str, set[int]]:
        return {m: {position(m, t) for t in tags} for m, tags in view.items()}

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

    # E14 oneStoreUnsettled: the unsettled events are all in one store and are the last
    # appended anywhere — so the stores' own cursors are the global settled prefix.
    unsettled = [e for e in log if not store.settled(e)]
    if len({e.domain for e in unsettled}) > 1:
        errors.append(f"oneStoreUnsettled: stores {sorted({e.domain for e in unsettled})}")
    if [e.pos for e in unsettled] != list(range(len(log) - len(unsettled), len(log))):
        errors.append("oneStoreUnsettled: an unsettled event precedes a settled one")
    # ...and an event lives in its procedure's domain's store, as does its command.
    for e in log:
        c = commands[e.command_id]
        if e.domain != g.domain(c.proc) or c.domain != g.domain(c.proc):
            errors.append(f"stores: {c.proc}'s event is in {e.domain}, its command in {c.domain}")

    # E9 dispatchComplete: every reacting policy of every settled event has its commands.
    dispatched = {(c.cause_event, c.cause_policy, c.proc) for c in commands.values()}
    for e in log:
        if not store.settled(e):
            continue
        for y in g.policies:
            if e.kind in g.reacts[y]:
                for q in g.dispatches[y]:
                    if (e.id, y, q) not in dispatched:
                        errors.append(f"dispatchComplete: {y} never dispatched {q} for {e.pos}")

    # E13 readOnlyConstant: nothing folds a read-only model; the read model holds exactly
    # its seed; every procedure and dispatching policy that read one read exactly that.
    for m in g.read_only:
        if g.folds[m]:
            errors.append(f"readOnlyConstant: {m} is read-only but folds {g.folds[m]}")
        if model is not None and set(model[m]) != {seed_tag(m)}:
            errors.append(f"readOnlyConstant: {m} holds {sorted(model[m])}, not its seed")
        for e in log:
            c = commands[e.command_id]
            if m in g.reads[c.proc] and positions(e.view)[m] != {SEED}:
                errors.append(f"readOnlyConstant: {c.proc} at {e.pos} read {m} as {e.view[m]}")
            if c.origin == "policy" and m in g.reads[c.cause_policy]:
                if positions(e.dispatch_view)[m] != {SEED}:
                    errors.append(f"readOnlyConstant: {c.cause_policy} read {m} unseeded")

    # E1 modelsAreFold, at idle: the read model is the fold of the whole log, over the seed.
    if model is not None:
        for m in g.models:
            want = {e.tag for e in log if e.kind in g.folds[m]}
            want |= {seed_tag(m)} if m in g.read_only else set()
            if set(model[m]) != want:
                errors.append(
                    f"modelsAreFold: {m} holds {len(set(model[m]))} tags, the fold {len(want)}"
                )

    # E15 foldSeqSound and E17 foldsInLogOrder (rule 7, nothing declared order-free): each
    # event is folded once, and a model fed by one store holds its events in append order
    # — however many reopens refolded it. (A model fed by two stores may not: that is the
    # reopen harm, which rule 7 exists to keep away from decisions.)
    if model is not None:
        pos_of = {e.tag: e.pos for e in log}
        for m in g.models:
            if m in g.read_only:
                continue
            seq = [pos_of.get(t, UNKNOWN) for t in model[m]]
            if len(seq) != len(set(seq)):
                errors.append(f"foldSeqSound: {m} folded an event twice: {seq}")
            stores = {g.domain(p) for p in g.procs if set(g.emits[p]) & set(g.folds[m])}
            if len(stores) <= 1 and seq != sorted(seq):
                errors.append(f"foldsInLogOrder: {m}, fed by one store, folded as {seq}")

    return errors


def check_idle(g: Graph, store: Store, model: dict[str, set[str]]) -> list[str]:
    """``check`` plus what an idle engine owes: nothing queued or running, all settled."""
    errors = check(g, store, model)
    behind = [e.pos for e in store.log if not store.settled(e)]
    if behind:
        errors.append(f"idle with events {behind} unsettled")
    stuck = [c.proc for c in store.commands.values() if c.status != "done"]
    if stuck:
        errors.append(f"idle with commands not done: {stuck}")
    return errors
