# dizzy-topology — project guide

Deployment topology for DIZZY features, as two packages in one uv workspace. Read
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

`just test` (both packages), `just lint`, `just foogle` (DIZZY's conformance scenario on
the topology engine: inline must match the golden log byte for byte; pooled must hold its
facts, crash runs included). `just test-update` re-snapshots intentionally.

Pool tests spawn processes. A pool-side callable (`pool_domains`) must be module-level so
it pickles by reference. That is why the engine tests' app lives in
`engine/tests/_app.py` and `conftest.py` puts it on `sys.path`.
