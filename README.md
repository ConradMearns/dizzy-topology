# dizzy-quint

Formal specifications of [Dizzy](https://github.com/ConradMearns/dizzy) engines in
[Quint](https://quint-lang.org/), and the harness that checks the Python engines against them.

The thesis: define Dizzy's semantics at the *weakest* topology — every edge async,
projections settle with arbitrary lag — and treat the single-threaded engine as the
degenerate case where lag is zero. A feature-file that is correct under those semantics
is correct under every rule-6-legal segmentation, so the topology engine becomes an
optimiser rather than a correctness gatekeeper.

The design notes this rests on are in the submodule: `dizzy/docs/explanation/topology.md`.

## Layout

| file | what |
|---|---|
| `specs/graph.qnt` | The abstract shape of a feature: `reads / emits / folds / reacts / dispatches / interactive` over a bounded universe. Models are sets of folded log positions; events carry the views their emitter and dispatcher read. Every engine draws a graph nondeterministically at `init`, so a check ranges over every feature of that size. Also holds the log predicates every engine shares (`freshReadsOf`, `causeFoldedOf`, …). |
| `specs/st.qnt` | **Spec 1** — the single-threaded engine. `run_command` is one atomic step; `cursor == log.length()` is an invariant. At-least-once redelivery is modelled as the code has it; `guarded` (application dedupes on ids) is a nondet named assumption. |
| `specs/groups.qnt` | **Spec 2** — consumer groups. Same graph split across groups with per-group cursors and lanes, shared substrate, cursor loss, redelivery. `claim` and `execute` are separate steps because `GroupWorker` has no lock between them — and `witnessClaimWindow` shows that a rule-6-legal split can therefore still read behind the tail. The provable theorem is `rule6 ⇒ causeFolded ∧ dispatchCauseFolded` — what `groups.py:27-29` promises — and `witnessWriteSkew` shows two rule-6-legal decisions that never saw each other. Exactly-once is a graph property: only `guardable` procedures (they read a model that folds their own emits) can dedupe a replay, and only with rule 6. |
| `specs/named/` | The first, concrete feature (`register`/`welcome`) kept as a readable witness. |
| `dizzy_topology.qnt` | Motivating example: unique-username registration under unsafe vs safe topologies. |
| `report/` | `run_checks.py` runs every simulation / witness / Apalache check into `results.json`; `render.py` renders `index.html`. |
| `dizzy/` | Git submodule, pinned to the dizzy commit the specs were written against. |

## Setup

```
git submodule update --init
bun add -g @informalsystems/quint     # or npm i -g; Apalache is fetched on first `quint verify` (needs Java 17)
sd list                                # roadmap lives in .seeds/
```

## Run

```
python3 report/run_checks.py            # ~10 min; add --no-verify to skip Apalache, --quick for fewer samples
python3 report/render.py                # → report/index.html
```

Apalache depth is exponential here (st: 4 s / 17 s / 5 min at depths 2 / 3 / 4), so the suite proves depth 4 for `st` and 3 for `groups`; simulation covers 3000 traces × 20 steps.

## Roadmap

Tracked in `.seeds/` — `sd ready` shows what is unblocked. Increments: st → consumer
groups (rules 1/3/6 as a predicate, refinement of st) → distributed faults → trace
schema + trace validation of the Python engines → adversarial-scheduler engine.
