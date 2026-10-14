# dizzy-topology

Deployment topology for [DIZZY](https://github.com/ConradMearns/dizzy) features: **where**
a feature's elements run, never **what** they do (that is the feature-file). Two packages,
one on each side of the build/run line, and the formal specification that proves the
engine correct:

| Package | When | Depends on | What it does |
|---|---|---|---|
| `topology/` → `dizzy-topology` | build time | DIZZY | Validates a `topology.yaml` against the feature-file, resolves it to `placement.json`, profiles a captured run. |
| `engine/` → `dizzy-topology-engine` | run time | `dizzy_kit`, `dizzy_engine` (what `dizzy generate` vendors into every app). **Never DIZZY** (a test enforces it). | The topology engine: the Simple Engine with lanes and process pools. |
| `spec/` | — | Quint (+ Apalache) | The engine's formal specification (`spec/specs/engine.qnt`) and the earlier `st`/`groups` specs, merged in from `dizzy-quint` with its history. See [`spec/README.md`](spec/README.md). |

The design is in [`docs/topology.md`](docs/topology.md). The Simple Engine spec it
builds on is `ext/dizzy/docs/design/simple-engine.md`, whose §10 names this package.

## Usage

```bash
just dizzy-init      # after cloning: fetch ext/dizzy at the pinned commit, vendor its runtime
uv sync

uv run dizzy-topology check app.feat.yaml topology.yaml
uv run dizzy-topology place app.feat.yaml topology.yaml -o placement.json
```

```python
from dizzy_topology_engine import Placement, TopologyEngine

engine = TopologyEngine(
    data_dir,
    [wiring.build_domain(resources)],  # the generated wiring, as for the Simple Engine
    Placement.load("placement.json"),
    pool_domains=build_pool_domains,  # module-level: what a pool worker builds
    commit=session.commit,
)
engine.submit(command)
engine.run()
```

## How the engine works

- **An `inline` lane runs the Simple Engine's own unit, inherited and not reimplemented.**
  With no pools and no interactive commands, the topology engine writes the Simple
  Engine's log byte for byte.
- **A `pool` lane** runs a procedure in a spawned process pool, so a slow procedure no
  longer holds the head of the queue. The child returns what the procedure emitted. The
  parent then appends, settles and acks it. **The thread that calls `run()` is the only
  writer.**
- **Claim order:** interrupted attempts first (re-run under the same attempt id), then
  `interactive` commands, then enqueue order. Only lanes with a free worker can claim.
- **Same store format.** A store this engine wrote opens in any Simple Engine, and the
  other way round.
- **Failures are never retried automatically** (Simple Engine §11.2). A procedure that
  raises, or a pool worker that dies, fails the attempt, and the host calls `retry`.
  Workers exit when their host dies.

## Correctness

Three layers, each checking the one below it (details in [`spec/README.md`](spec/README.md)):

1. **The design is proved.** `spec/specs/engine.qnt` models the engine's transactions,
   both lanes, crashes between any two transactions, element failure and host retry.
   Apalache proves all 13 invariants over every feature graph × rule-3 placement of the
   bounded size, to depth 6. Among them:
   - **exactly-once effects, unconditionally**, because append and ack are one
     transaction. The old `st` engine needed an application guard.
   - **fresh reads with pools**, although the log leaves claim order.
   - **fresh, complete dispatch across crashes.**
2. **The invariants have teeth.** All 13 witnesses are reachable. Mutating the spec into
   the old `st` engine's behaviour, or letting claims skip the settle, breaks it.
3. **The code satisfies the same invariants.** `engine/tests/test_spec_conformance.py`
   runs the real `TopologyEngine` on random graphs from the spec's universe. It crashes it
   at every claim/append/enqueue point and checks the store against a port of the Quint
   predicates. Splitting the ack from the append in the Python engine fails it.

## Checks

- `just test` — the validator, placement and profiler suites (ported from v2), and the
  engine suite: inline matches the Simple Engine exactly, pools, priority, recovery,
  failure and retry, workers dying with their host, and the import boundary.
- `just spec-engine` — simulate `engine.qnt`, then prove it to depth 5 (~1 min).
  `just spec` checks every spec quickly. `just spec-full` runs every proof and renders
  `spec/report/index.html`.
- `just foogle` — DIZZY's conformance scenario on the topology engine. All-inline must
  reproduce DIZZY's golden log byte for byte, crash runs included. The pooled topology
  (`engine/conformance/foogle.topology.yaml`) must reproduce the golden's facts and read
  model, also across crash runs.

## Migration from `engine-topology-v2`

That branch forked DIZZY at `2262b04` (0.1.1). Since then main has **replaced the engine
the branch extended** (`dizzy.engine.loop`, `st`, `mp`, the DAG store, `HostApp`) with the
Simple Engines. So the runtime half is being **re-founded** on the Simple Engine store
format, not copied over.

| v2 piece | Status |
|---|---|
| `def/topology.yaml`, `topology_schema.py`, `topology_loader.py` (rules 1–6, waivers) + tests | **Migrated** → `topology/` |
| Lane placement (`LANES`/`COMMAND_PLACEMENT` emitted into the wiring) | **Migrated** as `dizzy-topology place` → `placement.json`. Keyed on command feat name, not class name. DIZZY's wiring generator is untouched. |
| `st/executor.py` LaneExecutor: inline + pool lanes, one writer, crash → re-run | **Re-founded** as `TopologyEngine` |
| Two-level command priority (`st/command_queue`) | **Re-founded** (claim order) |
| Queue intent dedup, receipts, typed no-result outcomes | **Covered by the Simple Engine:** derived/host-supplied command ids, the `attempts` table, `declined` + reason |
| `cursors.py` cursor store | **Covered by the Simple Engine** `cursors` table, for the one cursor per domain it has |
| `groups.py` consumer-group worker (per-group cursors, doorbell transports). v2 itself never wired it. | **Not started.** Needs per-group cursors on the shared store. |
| `max_in_flight`, `transport`, `cursors:` topology fields | **Validated but not executed** yet |
| `retry:` topology field (automatic retry with backoff) | **Open question.** Conflicts with Simple Engine §11.2 and DIZZY's rule that a retry is a declared reaction. |
| `profiler.py` + `profile` verb | **Migrated**, but its input (`JsonlBusSink` records) has no producer on main. The Simple Engine's trace spans (`telemetry/*.jsonl`, with per-element `duration_ms`) are the likely replacement. |
| `dizzy-quint` (Quint specs of `st` and `groups`) | **Merged** into `spec/` with history. `st.qnt` is kept as the baseline. `groups.qnt` is the design spec for consumer groups when they are re-founded. New: `engine.qnt` and the conformance bridge. |
| Telemetry injection, query-access shapes, `JsonlBusSink` | **Not started.** The Simple Engine has its own trace (spec §9). |
| Feat validation rules (unique names per section, one procedure per command, command class collisions) | **Belongs upstream** in DIZZY's `validate_feat`. Not on main. |
| Tutorial, `examples/assay` | **Not migrated.** They run on the old engine. foogle is the conformance scenario for now. |

Read the v2 sources with `git -C ext/dizzy show origin/engine-topology-v2:<path>`.
