# dizzy-topology

Deployment topology for [DIZZY](https://github.com/PNNL/dizzy) features: **where**
a feature's elements run, never **what** they do (that is the feature-file). Two packages,
one on each side of the build/run line, and the formal specification that proves the
engine correct:

| Package | When | Depends on | What it does |
|---|---|---|---|
| `topology/` → `dizzy-topology` | build time | Nothing of DIZZY: it reads the slice of a feature-file it needs itself (`feat.py`; a test enforces it). | Validates a `topology.yaml` against the feature-file, resolves it to `placement.json`, installs the engine into a generated app (`vendor`), profiles a captured run. |
| `engine/` → `dizzy-topology-engine` | run time | `dizzy_kit`, `dizzy_engine` (what `dizzy generate` vendors into every app). **Never DIZZY** (a test enforces it). | The topology engine: the Simple Engine with lanes and process pools. |
| `spec/` | — | Quint (+ Apalache) | The engine's formal specification (`spec/specs/engine.qnt`) and the earlier `st`/`groups` specs, merged in from `dizzy-quint` with its history. See [`spec/README.md`](spec/README.md). |

The design is in [`docs/topology.md`](docs/topology.md). The Simple Engine spec it
builds on is DIZZY's [`docs/design/simple-engine.md`](https://github.com/PNNL/dizzy/blob/main/docs/design/simple-engine.md),
whose §10 names this package.

DIZZY itself is needed only to develop and test this repo: the engine's tests run on
DIZZY's run-time packages, and `just foogle` / `just assay` run `dizzy generate`.
`just dizzy` fetches DIZZY at the commit pinned in the `justfile` (`dizzy_rev`) into
`.dizzy/`, and copies its run-time packages into `vendor/python/` as `dizzy generate
wiring` copies them into an app. `just test`, `just foogle` and `just assay` run it
first. Set `DIZZY_REPO` to fetch from elsewhere, e.g. a local checkout of DIZZY you are
changing alongside this repo.

## Using it in a DIZZY Python app

[`examples/assay`](examples/assay) is the worked example, and `just assay` runs every step
below from a clean copy. Set this repo up once:

```bash
git clone <this repo> && cd dizzy-topology
just dizzy && uv sync
```

**1. Generate the app**, as for any DIZZY Python app. The app runs inside the uv
workspace this writes at `<app>/lib/python-uv/`. (`D` is this repo. Any DIZZY with the
Simple Engine works; this uses the pinned one `just dizzy` fetched.)

```bash
uv run --project $D/.dizzy dizzy generate static    app.feat.yaml <app>
uv run --project $D/.dizzy dizzy generate libraries app.feat.yaml <app>
uv run --project $D/.dizzy dizzy generate wiring    app.feat.yaml <app>
```

**2. Install the engine into it.** DIZZY's runtime packages aren't on a package index:
`generate wiring` vendors the Simple Engine into the workspace as a member. `vendor`
does the same for the topology engine, at `lib/python-uv/dizzy-topology-engine/`. Re-run
it after `generate libraries`, which rewrites the members list.

```bash
uv run --project $D dizzy-topology vendor <app>
```

**3. Say where things run, and check it.** Write a `topology.yaml`
([`docs/topology.md`](docs/topology.md); the assay one is annotated). `place` validates
it against the feature-file and resolves it to the data the engine reads at run time:

```bash
uv run --project $D dizzy-topology place app.feat.yaml topology.yaml -o <app>/placement.json
```

**4. Run it.** The host is the Simple Engine's, with a placement and one more callable.
It runs inside the app's workspace (`uv run --project <app>/lib/python-uv python run.py`):

```python
from dizzy_topology_engine import Placement, TopologyEngine

def pool_domains():  # module-level: each pool worker calls it once, in its own process
    return [wiring.build_domain(wiring.Resources(...))]  # no read model; read-only copies

engine = TopologyEngine(
    data_dir,
    [wiring.build_domain(resources)],  # exactly what the Simple Engine takes
    Placement.load("placement.json"),  # None: everything inline, the Simple Engine's order
    pool_domains=pool_domains,
    commit=session.commit,
)
engine.submit(command, id="...")
engine.run()  # to idle
```

The store is the Simple Engine's. A store this engine wrote opens in any Simple Engine,
and the other way round.

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
- **Several domains** (a core feat and its plugins, Simple Engine §2) each keep their own
  store: a command and the events its procedure appends live in the store of that
  procedure's domain. Projections and policies react to events from any domain, and one
  `seq` across the stores orders every claim. That includes re-running interrupted
  attempts, which can sit in several stores at once when pools are in play.
- **Failures are never retried automatically** (Simple Engine §11.2). A procedure that
  raises, or a pool worker that dies, fails the attempt, and the host calls `retry`.
  Workers exit when their host dies.
- **Read-only models** are reference data: lookups that never update. A topology lists
  them in `read_only_models`; the build refuses one a projection folds. The host seeds
  them before the engine opens, and nothing writes them while it runs. A read of a model
  that never changes is the same wherever it runs, so a procedure whose queries read only
  read-only models may go on a pool. Each worker opens its own read-only copy through
  `pool_domains`. foogle's `gronk_values` is one: a SQLite file opened read-only by the
  host and by every worker, with `poke_snarf` on a pool.

## Telemetry

One trace per engine run, as in the Simple Engine (§9): JSON lines under
`<data>/telemetry/`, and OTLP/HTTP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. The
topology engine adds:

- **lanes on every unit:** lane, executor, priority, time queued, and whether it re-ran
  an interrupted attempt;
- **pooled units timed claim to settle,** with the worker's own time inside: waiting in
  the pool, the procedure in the worker (its pid), and the handoff back to the host;
- **pool and worker spans:** each pool's life, jobs and failures, and each worker's
  startup cost;
- **queue counters:** `dizzy.lane.queued` and `dizzy.lane.in_flight`, up/down counters
  per lane, store and priority, with one exact point per change (OTLP metrics, and JSON
  lines under `<data>/telemetry/metrics/`).

```bash
docker run -d -p 4318:4318 -p 8000:8000 ghcr.io/ctrlspice/otel-desktop-viewer:latest --host 0.0.0.0
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 just assay   # http://localhost:8000
```

[`docs/telemetry.md`](docs/telemetry.md) has the span tree, the attributes, what each
answers when choosing a placement, the counters, and what isn't built yet (the profiler
reading these spans).

## Correctness

Four layers, each checking the one below it (details in [`spec/README.md`](spec/README.md)):

1. **The design is proved.** `spec/specs/engine.qnt` models the engine's transactions,
   both lanes, crashes between any two transactions, element failure and host retry.
   Apalache proves all 19 invariants over every feature graph × domain split × read-only
   declaration × rule-3 placement of the bounded size, to depth 6. Among them:
   - **exactly-once effects, unconditionally**, because append and ack are one
     transaction. The old `st` engine needed an application guard.
   - **fresh reads with pools**, although the log leaves claim order.
   - **fresh, complete dispatch across crashes.**
   - **read-only models never change**, across appends, settles and crashes, and every
     reader of one, pooled or not, read exactly what the host seeded.
   - **several domains recover correctly.** Each store keeps only its own events and
     cursor, and a reopen refolds store by store. That gives the same read model as the
     true interleaving, because at most one store is ever unsettled.
   - **...but not in the same order.** A model fed by two stores comes back reordered
     after every reopen, and a decision taken on it can differ from one taken without
     the reopen, if its projection cares about order. See *The reopen harm* below.
2. **The invariants have teeth.** All 16 witnesses are reachable. Mutating the spec into
   the old `st` engine's behaviour, or letting claims skip the settle, breaks it. So does
   declaring a folded model read-only, or a host that doesn't re-seed after a crash.
3. **The code satisfies the same invariants.** `engine/tests/test_spec_conformance.py`
   runs the real `TopologyEngine` on random graphs from the spec's universe. It crashes it
   at every claim/append/enqueue point and checks the store against a port of the Quint
   predicates. Splitting the ack from the append in the Python engine fails it, and so
   does a pool worker that doesn't seed its copy of a read-only model.
4. **Every step the code takes is a step the spec takes.**
   `engine/tests/test_mbt.py` is model-based testing. Quint generates 300 traces of the
   spec (`quint run --mbt`). Each is replayed on a real engine, held after every
   transaction. One abstraction function (`_mbt.py`) maps the engine's store and memory
   to the spec's variables, and the result must equal the trace's state after every
   step. This checks what layer 3 can't: claim order, which pool job commits when, what
   a crash leaves behind, and the elements' semantics. Mutants that pass layer 3 fail
   here: interactive claimed after batch, recovery after queued work, a pool lane one
   wider than placed, an ack outside the append's transaction, the old batch-commit
   loop, and re-running interrupted attempts store by store instead of in `seq` order.
   That last one was a real bug, found by the replay once the spec had two domains.

## The reopen harm (multi-domain fold order)

Every open rebuilds the read model from the stores (Simple Engine §4). Within one store,
the rebuild folds events in their original order. Across stores it can't: no store
records how they interleaved, so the core store is refolded first, then each plugin's.
A model fed by events from two stores therefore comes back **reordered** after any
restart, not only after a crash.

The model is fine if its projection doesn't care about order (a sum, a count). If it does
(last write wins, a status machine, a list), a procedure or policy that reads the model
after the restart sees a value no run without the restart could produce. It then emits a
different event, and no refold repairs the log.

`engine.qnt` investigates this with a fold-order ghost (`foldSeq`) and a decision
counter (`misreads`), assuming the worst case: every projection cares about order.

- **The harm is real.** `reopenReordersTest` scripts it in 13 steps. Biased sampling
  (`engineReopen`) reaches it too. The real engine reproduces it exactly
  (`test_a_reopen_reorders_a_model_fed_by_two_stores`), and the step-by-step replay
  checks the engine's fold order against the spec's through 10 reordering reopens.
- **A candidate rule 7 prevents it:** a model fed by more than one store must be declared
  order-independent. Under it, every other model stays in append order across any number
  of reopens (`foldsInLogOrder`), and no decision is taken on a reordered model
  (`readsInLogOrder`). A reopen that refolds a store in reverse breaks the first.
- **Limits of the evidence.** The harm needs about 13 steps, beyond Apalache's depth of 6,
  so the proof of rule 7 to depth 6 is vacuous. The evidence for it is the biased
  sampling, where the harm is reachable, and the step-by-step replay.
- **Not built yet:** enforcing rule 7 at build time. That needs `dizzy-topology` to read a
  core feature-file together with its plugins (`dizzy-topology-dd07`). The other fix,
  recording the cross-store order durably, would change the store format shared by
  DIZZY's three engines.

## Limitations

Known, deliberate or not yet done, in one place. Each links to where it's explained.

**What the proofs cover**
- **The proofs are bounded, not unbounded.** Apalache checks every behaviour up to 6
  steps, with 2 procedures, 1 policy, 2 models, 2 event kinds, 2 domains, 2 host
  submissions and 6 events. There's no inductive invariant yet. Simulation covers longer
  traces of the same universe ([`spec/README.md`](spec/README.md)).
- **Rule 7 rests on sampling, not proof.** The reopen harm takes about 13 steps, beyond
  that depth. The evidence for rule 7 is biased sampling, a scripted scenario, and its
  replay on the real engine (*The reopen harm*, above).
- **The spec abstracts some things.**
  - Models are sets, with fold order as a ghost.
  - Projections are always the core domain's.
  - A worker dying is modelled as one job failing; a real broken pool fails every job in
    flight on it.
  - The `declined` outcome isn't modelled.
- **The replay checks only runs the spec drives.** A run with real OS scheduling isn't
  checked step by step against the spec (`dizzy-quint-2630`), only at its end states.

**What the engine does**
- **A reopen reorders a model fed by two domains' stores.** If its projection is
  order-sensitive, a decision after a restart can differ from one without it. Rule 7
  (declare such a model order-independent) isn't enforced at build time, because
  `dizzy-topology` reads one feature-file, not a core and its plugins
  (`dizzy-topology-dd07`).
- **Read-only models are a promise.** The build checks that no projection folds one. It
  can't check that the host never writes one while the engine runs. The declaration
  lives in `topology.yaml`, not the feature-file (`dizzy-topology-1de8`).
- **Pool workers are expensive to start:** 240–380 ms each on the assay example, built
  on a lane's first claim (*Telemetry*).
- **Only part of a topology runs.** Consumer groups, `max_in_flight`, `transport`,
  `cursors:` and `retry:` are validated but not executed. `retry:` conflicts with the
  Simple Engine's no-automatic-retry rule (*Status*, below).

**Telemetry** ([`docs/telemetry.md`](docs/telemetry.md))
- **Two metrics, no logs:** the queue counters only, with no unit-count or duration
  metrics (spans hold those). A procedure's own logging isn't captured.
- **The profiler still reads `JsonlBusSink` records,** not these spans
  (`dizzy-topology-964a`).
- **Worker times assume one machine's clock.** A worker's process start is exact only on
  Linux.
- **A trace is whole only after the engine closes.**
- **The topology spans go through one adapter onto the Simple Engine tracer's
  internals.** A change to that tracer in DIZZY could break them, and the telemetry tests
  would show it.

## Checks

- `just test` — the validator, placement and profiler suites, and the
  engine suite: inline matches the Simple Engine exactly, pools, priority, recovery,
  failure and retry, workers dying with their host, and both import boundaries. Two
  drift guards hold the feat reader to DIZZY's feat schema at the pin.
  It also replays the spec's traces on the engine (`test_mbt.py`, needs `quint` on
  `PATH`; skipped without it).
- `just spec-engine` — simulate `engine.qnt`, then prove it to depth 5 (~1 min).
  `just spec` checks every spec quickly. `just spec-full` runs every proof and renders
  `spec/report/index.html`.
- `just assay` — the worked example from scratch: generate a DIZZY app, `vendor` the
  engine into it, place it, run it all inline and under its topology. Both runs must
  reach the same summary.
- `just foogle` — DIZZY's conformance scenario on the topology engine. All-inline must
  reproduce DIZZY's golden log byte for byte, crash runs included. The pooled topology
  (`engine/conformance/foogle.topology.yaml`) must reproduce the golden's facts and read
  model, also across crash runs. It puts `poke_snarf`, a reader of the read-only
  `gronk_values`, on a pool, and checks that the reference file refuses a write.

## Status

What runs, what is only validated, and what is open. The run-time half is built on the
Simple Engine store format.

| Piece | Status |
|---|---|
| `topology.yaml` schema and validator (rules 1–6, waivers) | **Done:** `topology/` |
| Lane placement | **Done:** `dizzy-topology place` → `placement.json`, keyed on the command's feat name. DIZZY's wiring generator is untouched. |
| Inline and pool lanes, one writer, crash → re-run | **Done:** `TopologyEngine` |
| Two-level command priority | **Done** (claim order) |
| Queue intent dedup, receipts, typed no-result outcomes | **Covered by the Simple Engine:** derived/host-supplied command ids, the `attempts` table, `declined` + reason |
| Durable cursors | **Covered by the Simple Engine** `cursors` table, for the one cursor per domain it has |
| Consumer groups (per-group cursors, doorbell transports) | **Not started.** Needs per-group cursors on the shared store. `spec/specs/groups.qnt` is their design spec. |
| `max_in_flight`, `transport`, `cursors:` topology fields | **Validated but not executed** yet |
| `retry:` topology field (automatic retry with backoff) | **Open question.** Conflicts with Simple Engine §11.2 and DIZZY's rule that a retry is a declared reaction. |
| `profile` verb | **Works**, but its input (`JsonlBusSink` records) has no producer in DIZZY. It should read the engine's trace spans (`telemetry/*.jsonl`, with lanes, pool waits and worker starts) instead (`dizzy-topology-964a`). |
| Telemetry | **Done** on the Simple Engine's trace (§9): lanes, pooled units claim to settle, worker time, pools and workers, and each lane's queue as up/down counters ([`docs/telemetry.md`](docs/telemetry.md)). |
| Telemetry injection into elements, query-access shapes | **Not started.** |
| Feat validation rules (unique names per section, one procedure per command, command class collisions) | **Belong upstream** in DIZZY's `validate_feat`. |
| `examples/assay` | **Done:** current DIZZY, this engine, installed with `dizzy-topology vendor`, plus a read-only reference panel. `just assay`. |
