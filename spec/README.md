# spec/ — formal specifications of the engine

[Quint](https://quint-lang.org/) specifications of DIZZY engines, the harness that
model-checks them, and (in `../engine/tests/`) the bridge that holds the real topology
engine to the same invariants. Merged in from `dizzy-quint` with its history
(`git log dizzy-quint/main`).

**How "the engine is correct" is established, in three layers:**

1. **The design is proved.** `specs/engine.qnt` models the topology engine as it ships:
   the Simple Engine store's transaction boundaries, inline and pool lanes, crashes
   between any two transactions, element failure and host retry. Apalache proves every
   invariant over every feature graph × rule-3 placement of the bounded size, to depth 6.
   Simulation covers thousands of 25-step traces.
2. **The invariants have teeth.** Every witness is reachable (nothing is vacuously true).
   Mutating the spec as the old `st` engine behaved (ack outside the append's
   transaction) breaks `effectsOnce`. Letting claims skip the settle breaks `freshReads`.
3. **The implementation satisfies the same invariants.**
   `engine/tests/test_spec_conformance.py` runs the real `TopologyEngine` on random graphs
   from the same universe (`_graph_app.py` gives every element the spec's semantics), with
   random rule-3 placements. It crashes at **every** claim/append/enqueue point and drains,
   then evaluates `_spec.py`, a line-for-line port of the Quint predicates, on the store
   the engine wrote. The split-ack mutant of the *Python* engine fails it.

The thesis carried over from dizzy-quint: define DIZZY's semantics at the *weakest*
topology (every edge async, projections settling with arbitrary lag) and treat the
single-threaded engine as the degenerate case where lag is zero. A feature-file that is
correct under those semantics is correct under every rule-6-legal segmentation, so the
topology engine becomes an optimiser rather than a correctness gatekeeper. The design
notes are in `../docs/topology.md`.

## Layout

| file | what |
|---|---|
| `specs/graph.qnt` | The abstract shape of a feature: `reads / emits / folds / reacts / dispatches / interactive` over a bounded universe. Models are sets of folded log positions; events carry the views their emitter and dispatcher read. Every engine draws a graph nondeterministically at `init`, so a check ranges over every feature of that size. Also holds the log predicates every engine shares (`freshReadsOf`, `causeFoldedOf`, …). |
| `specs/engine.qnt` | **Spec 4 — the topology engine as shipped** (`engine/src/dizzy_topology_engine`). Store (log, command table, cursor) vs process memory (read model, current unit, in-flight pool jobs). Steps: `claim`, `runInline` / `completePool` (append **and** ack, one transaction), `settleFold` (fold + policies enqueue with `INSERT OR IGNORE`), `advance` (cursor), `crash` (memory lost, refold to cursor), `failInline`/`failPool`, host `retry`. Proved: `effectsOnce` **unconditionally**, `freshReads` under rule 3 even though pools take the log out of claim order (`witnessOutOfOrder`), `freshDispatch`/`dispatchComplete` across crashes including re-settles (`witnessResettle`). `witnessStalePool`: rule 3 is necessary. |
| `specs/st.qnt` | **Spec 1** — the pre-Simple-Engine single-threaded engine (`dizzy.engine.st`, removed from DIZZY main; kept as the baseline `engine.qnt` improves on). `run_command` is one atomic step; `cursor == log.length()` is an invariant. At-least-once redelivery is modelled as the code has it; `guarded` (application dedupes on ids) is a nondet named assumption. |
| `specs/groups.qnt` | **Spec 2** — consumer groups (v2's `GroupWorker`, not yet re-founded on the Simple Engine; this is its design spec). Same graph split across groups with per-group cursors and lanes, shared substrate, cursor loss, redelivery. `claim` and `execute` are separate steps because `GroupWorker` has no lock between them — and `witnessClaimWindow` shows that a rule-6-legal split can therefore still read behind the tail. The provable theorem is `rule6 ⇒ causeFolded ∧ dispatchCauseFolded` — what `groups.py:27-29` promises — and `witnessWriteSkew` shows two rule-6-legal decisions that never saw each other. Exactly-once is a graph property: only `guardable` procedures (they read a model that folds their own emits) can dedupe a replay, and only with rule 6. |
| `specs/feature.qnt` | The first, concrete feature (`register`/`welcome`) kept as a readable witness. |
| `dizzy_topology.qnt` | Motivating example: unique-username registration under unsafe vs safe topologies. |
| `report/` | `run_checks.py` runs every simulation / witness / Apalache check into `results.json`; `render.py` renders `index.html`. |
| `../engine/tests/_spec.py`, `_graph_app.py`, `test_spec_conformance.py` | The bridge: the spec's graph semantics as real Domain tables, the spec's invariants over a real store, and the crash sweep. |

## Setup

```
bun add -g @informalsystems/quint     # or npm i -g; Apalache is fetched on first `quint verify` (needs Java 17)
sd list                                # the roadmap lives in the repo-root .seeds/
```

## Run

From the repo root:

```
just spec-engine      # engine.qnt: simulate every invariant, then prove to depth 5 (~1 min)
just spec             # every spec, every invariant and witness, simulation only (--quick, ~2 min)
just spec-full        # 8000 samples per check + every Apalache proof, then report/index.html (~15 min)
just test             # includes the engine ↔ spec conformance suite
```

`--quick` (300 samples) is too few to reach four of `groups.qnt`'s witnesses
(`witnessClaimWindow`, `witnessWriteSkew`, `witnessStaleRead`, `witnessStalePolicy`); the
full 8000 reach them all.

`engine.qnt` proofs on this machine: depth 3 in 11 s, 4 in 19 s, 5 in 44 s, 6 in 3.7 min.
For `st`: 4 s / 17 s / 5 min at depths 2 / 3 / 4, so the suite proves depth 4 for `st` and
3 for `groups`. Simulation covers 8000 traces × 20 steps (25 for `engine.qnt`, whose
crashes and re-settles need the extra steps).

## Scale

`scale/gen.py` regenerates the universe block of `graph.qnt` at any size (procedures, policies, models, event kinds, groups) with out-degree ≤ 2 per element — real feature files are sparse, and an unbounded powerset makes a 100-procedure universe dispatch ~50 commands per event. `st.qnt` / `groups.qnt` are copied verbatim, so every rung checks the same semantics. `scale/bench.py` runs the ladder and records throughput and peak RSS in `scale/results.json` (rendered into the ledger).

| rung | procs / policies / models / kinds / groups | st | groups |
|---|---|---|---|
| p2 | 2 / 1 / 2 / 2 / 2 | 236 traces/s · Apalache depth 4 | 399 traces/s · Apalache depth 3 |
| p5 | 5 / 2 / 3 / 3 / 2 | 88 · Apalache depth 2 (5 min) | 355 · Apalache depth 2 |
| p10 | 10 / 4 / 4 / 4 / 3 | 36 · Apalache depth 1 (100 s, 24 GB heap) | 267 · Apalache depth 1 |
| p25 | 25 / 8 / 6 / 4 / 3 | 11 · Apalache infeasible (JVM > 46 GB) | 96 · Apalache depth 1 (13.5 min, 24 GB heap) |
| p50 | 50 / 16 / 8 / 6 / 4 | 6 | 61 |
| p100 | 100 / 32 / 12 / 8 / 4 | 3 (TypeScript backend) | 45 |
| p200 | 200 / 64 / 16 / 10 / 6 | — (backend error) | 18 |
| p400 | 400 / 128 / 24 / 12 / 8 | — | — (backend error) |

All rows are 300 traces × 20 steps, all invariants holding. Peak memory stays under 750 MB. The ceiling at p200–p400 is quint's simulators (the Rust one crashes, the TypeScript one runs out of heap), not the machine; Apalache's default 4 GB JVM heap OOMs from p25; with `JVM_ARGS=-Xmx24G` (`scale/apalache_heap.sh`) depth 1 is provable at p10 for `st` and p25 for `groups`, and that is the proof ceiling on this machine — p50 `groups` ran 5.5 h without finishing and p25 `st` exceeded 46 GB resident.

## Roadmap

Tracked in the repo-root `.seeds/` — `sd ready` shows what is unblocked. Increments: st → consumer
groups (rules 1/3/6 as a predicate, refinement of st) → distributed faults → trace
schema + trace validation of the Python engines → adversarial-scheduler engine.
