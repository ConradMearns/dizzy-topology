# dizzy-topology — project guide

Deployment topology for DIZZY features, as two packages in one uv workspace, plus the
engine's formal specification in `spec/` (Quint; merged in from `dizzy-quint`). Read
`README.md` first: it has the layout, how the engine works, and the migration status from
DIZZY's `engine-topology-v2` branch. `docs/topology.md` is the design.

## Boundaries (the reason this repo exists)

- **`topology/` (`dizzy_topology`) is build time.** It depends on DIZZY and may import
  any of it (`feat_schema`, `feat_loader`, `validation`, `logger`). What it hands to run
  time is data: `placement.json` (`dizzy-topology place`).
- **`engine/` (`dizzy_topology_engine`) is run time, a dependency of apps built on
  DIZZY.** It may import only `dizzy_kit` and `dizzy_engine`, which `dizzy generate`
  vendors into every app. It never imports DIZZY or `dizzy_topology`
  (`engine/tests/test_boundary.py` enforces this). It never reads a feat or a
  topology.yaml, only a placement.
- **The engine builds on the Simple Engine** (`ext/dizzy/docs/design/simple-engine.md`;
  §10 names this package). It keeps the same store format, ids and `Domain` tables. An
  inline lane is the Simple Engine's own `_unit`, inherited. Every store write happens on
  the thread that calls `run()`; pool children are pure.
- DIZZY's rules apply here too (`ext/dizzy/CLAUDE.md`, "The rule"): no shadow state, no
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
plus this repo's own. DIZZY's tracker is separate: `just dizzy-seeds ...`.

## DIZZY submodule (`ext/dizzy`) and `vendor/`

DIZZY is a git submodule at `ext/dizzy`. Its commit pointer is the version pin.

- After cloning, or if `ext/dizzy` is empty: `just dizzy-init` (also runs `just vendor`).
- `vendor/python/{dizzy-kit,dizzy-engine}` are copied from the submodule by `just vendor`,
  as `dizzy generate wiring` copies them into an app. They are gitignored. **Re-run
  `just vendor` after bumping `ext/dizzy`.** They can't be workspace members in place: uv
  stops at `ext/dizzy` (a non-workspace project) and their `workspace = true` sources fail.
- Before editing DIZZY: `just dizzy-dev` (checks out a branch, so commits don't land on a
  detached HEAD). Push and re-pin: `just dizzy-push`, then commit the pointer here.
  **Always push the submodule before pushing this repo.**
- DIZZY's own issues go in its tracker: `just dizzy-seeds ...`.

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
