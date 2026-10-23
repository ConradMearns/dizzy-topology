# dizzy-topology — project guide

Deployment topology for DIZZY features, as two packages in one uv workspace, plus the
engine's formal specification in `spec/` (Quint; merged in from `dizzy-quint`). Read
`README.md` first: it has the layout, how the engine works, and the status of each
piece. `docs/topology.md` is the design.

## Boundaries (the reason this repo exists)

- **`topology/` (`dizzy_topology`) is build time.** It depends on no DIZZY package
  (`topology/tests/test_boundary.py` enforces this). It reads the slice of a feat a
  placement needs itself (`feat.py`) and ignores the rest. DIZZY owns the feat format
  and validates it. Two drift guards in `test_topology_loader.py` hold `feat.py` to
  DIZZY's feat schema at the pin. What it hands to run time is data: `placement.json`
  (`dizzy-topology place`).
- **`engine/` (`dizzy_topology_engine`) is run time, a dependency of apps built on
  DIZZY.** It may import only `dizzy_kit` and `dizzy_engine`, which `dizzy generate`
  vendors into every app. It never imports DIZZY or `dizzy_topology`
  (`engine/tests/test_boundary.py` enforces this). It never reads a feat or a
  topology.yaml, only a placement.
- **The engine builds on the Simple Engine** (`.dizzy/docs/design/simple-engine.md`;
  §10 names this package). It keeps the same store format, ids and `Domain` tables. An
  inline lane is the Simple Engine's own `_unit`, inherited. Every store write happens on
  the thread that calls `run()`; pool children are pure.
- DIZZY's rules apply here too (`.dizzy/CLAUDE.md`, "The rule"): no shadow state, no
  hidden reactions (so no automatic retry), and an engine interprets a feat and is never
  one itself.
- `topology/src/dizzy_topology/topology_schema.py` is generated. Edit
  `def/topology.yaml` and run `just gen-topology-pydantic`.

## Telemetry

The engine's spans are the Simple Engine's trace (spec §9) plus lanes, pools and workers,
and two up/down counters per lane (`dizzy.lane.queued`, `dizzy.lane.in_flight`;
`docs/telemetry.md`). Two rules: no decision ever reads telemetry (the `_claims` and
`_pool_spans` bookkeeping and the counters are write-only), and the engine reaches past the tracer's `span`
only in `engine/src/dizzy_topology_engine/telemetry.py`. `engine/tests/test_telemetry.py`
pins the span tree. A worker reports its times in `Outcome`, never through telemetry
of its own.

## The spec is part of the engine

`spec/specs/engine.qnt` specifies `engine/src/dizzy_topology_engine` at the level of its
store transactions. **Change one, change the other in the same commit.**

- Changing when the engine claims, appends, acks, settles or advances the cursor, or what
  survives a crash, means changing `engine.qnt`. Re-run `just spec-engine`: the invariants
  must still hold and every witness must still be reachable.
- `engine/tests/_spec.py` ports the Quint invariants to Python, checked against the real
  engine's store by `test_spec_conformance.py`. A new invariant in `engine.qnt` gets a
  port there.
- `engine/tests/test_mbt.py` replays Quint's traces of `engine.qnt` on the real engine
  and compares the state after **every step** (`_mbt.py`). Each spec action ends at one
  engine pause (`_ENDS_AT`), and `Driver.abstract` maps the engine to the spec's
  variables. A new spec variable or action needs both updated. A replay failure names the
  step where the engine and the spec diverged. Fix whichever one is wrong. Don't loosen
  the comparison.
- With several domains the spec's `log` is the append order ACROSS stores, which the
  engine never records (each store keeps its own chain). The test engines record it
  (`_graph_app.RecordingEngine`, `append-order.jsonl` beside the stores) so `_spec.py` and
  `_mbt.py` can interleave the stores. Test instrumentation only: never add it to the
  engine.
- `engine.qnt` is `engineSpec` with its bounds as constants, instantiated as `engine`
  (proved; the default main) and `engineNarrow` (a one-wide pool, for the replay).
- A spec change that weakens an invariant needs a reason in the commit message. Proving
  less is a design decision, not a fix.
- Crash coverage is a sweep, never a sample: a bug between two transactions is one crash
  point among dozens.
- `spec/report/results.json` and `index.html` are committed outputs of `just spec-full`.
  `spec/report/logs/` is not.

## Issue tracking

`sd` (Seeds) at the repo root: the roadmap merged from dizzy-quint (ids `dizzy-quint-*`)
plus this repo's own. DIZZY's tracker is separate and lives in DIZZY's repo.

## DIZZY (`.dizzy/`) and `vendor/`: development and tests only

Neither package depends on DIZZY. This repo needs it for two things: the engine's
tests run on DIZZY's run-time packages, and `just foogle` / `just assay` run
`dizzy generate`. The pin is `dizzy_rev` in the `justfile`, a commit on PNNL/dizzy.

- `just dizzy` fetches DIZZY at the pin into `.dizzy/` and copies
  `vendor/python/{dizzy-kit,dizzy-engine}` out of it, as `dizzy generate wiring` copies
  them into an app. Both are gitignored. `just test`, `lint`, `foogle` and `assay` run
  it first. It is a no-op when both are at the pin. Run it after cloning and after
  bumping `dizzy_rev`.
- The run-time packages can't be git sources: their `workspace = true` sources fail
  outside a workspace. That is why they're copied in as workspace members.
- `dizzy generate` runs in DIZZY's own locked env (`uv run --project .dizzy`), never in
  this workspace.
- To change DIZZY alongside this repo: commit in a DIZZY checkout, point `dizzy_rev` at
  the commit and fetch with `DIZZY_REPO=<path to the checkout> just dizzy`. **Push the
  commit to PNNL/dizzy before pushing a re-pin here.** Without it nobody else can fetch
  the pin.

## Quality gates

`just test` (both packages, including the spec conformance sweep and the step-by-step
replay of the spec's traces, which needs `quint` on `PATH`), `just lint`,
`just spec-engine`, `just assay` (the worked example, installed into a generated app
with `dizzy-topology vendor`), `just foogle` (DIZZY's conformance scenario on
the topology engine: inline must match the golden log byte for byte; pooled must hold its
facts, crash runs included). `just test-update` re-snapshots intentionally.

Pool tests spawn processes. A pool-side callable (`pool_domains`) must be module-level so
it pickles by reference. That is why the engine tests' app lives in
`engine/tests/_app.py` and `conftest.py` puts it on `sys.path`.
