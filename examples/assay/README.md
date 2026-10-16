# assay: a DIZZY Python app on the topology engine

A fan-out/fan-in scientific workflow, and the worked example of everything this repo
does for an app. A DIZZY feature is generated into a Python app. `dizzy-topology vendor`
installs the topology engine into it. A `topology.yaml` says where each element runs,
and `run.py` runs it.

```
ingest_run ─▶ run_parsed ─┬─▶ split_run ─▶ sample_extracted × N     (main, inline)
                          └─▶ render_plot ─▶ plot_rendered          (compute, pool×4)
sample_extracted ─┬─▶ analyze_sample  ─▶ sample_analyzed            (compute, pool×4)
                  └─▶ classify_sample ─▶ sample_classified          (classify, pool×1)
last result home  ─▶ summarize_run ─▶ run_summarized                (main, inline)
```

One ingested instrument run fans out into N samples. Each sample is analyzed (real
CPU-bound arithmetic) and classified (an exclusive instrument, labelled from a reference
panel) on pool lanes. A policy fires the summary exactly once, when the last result lands.

## Run it

From the repo root, `just assay` does all of this in `.build/assay`, from scratch:

```bash
A=.build/assay && cp -r examples/assay $A
uv run dizzy generate static    $A/assay.feat.yaml $A      # types and the kit
uv run dizzy generate libraries $A/assay.feat.yaml $A      # element packages (keeps the bodies)
uv run dizzy generate wiring    $A/assay.feat.yaml $A      # wiring and the Simple Engine
uv run dizzy-topology vendor $A                            # the topology engine, as a member
uv run dizzy-topology place $A/assay.feat.yaml $A/topology.yaml -o $A/placement.json
uv run --project $A/lib/python-uv python $A/run.py                                # all inline
uv run --project $A/lib/python-uv python $A/run.py --placement $A/placement.json  # placed
```

Both runs print the same summary. `run.py` checks it against the manifest (every sample
analyzed and classified, the hit count, the labels), and the placed run is faster:

```
lanes:   main inline×1, compute pool×4, classify pool×1, default inline×1
ran:     52 units in 1.61s
summary: 24 analyzed, 24 classified, 12 hits, mean metric 0.6682250000
OK
```

## The topology

The feature-file says *what* each element does, and `topology.yaml` says *where* it runs.
`dizzy-topology place` refuses a placement that breaks a rule, naming the rule.

- **`compute` is the wide CPU pool.** `compute_metrics` reads nothing: the
  `fan_out_analysis` policy copies the measurement into the command, so the read moves to
  the dispatcher. Rule 3 lets it fork across four worker processes.
- **`classify` is the exclusive instrument.** It's a pool with one worker, so its jobs
  stay serial against the resource without ever blocking a fold. `classify_sample` does
  read a model, the reference **panel**, but the topology declares that model read-only
  (`read_only_models: [panel]`). Reference data that never changes reads the same on
  any lane, so rule 3 allows it. Delete that line and `place` refuses the topology.
- **`main` holds every fold, every policy, and the reader of the ledger.**
  `summarize_run` queries the ledger, so rule 6 pins it to the lane whose fold it
  trails. The finalize policies read the same ledger and live there for the same reason.

## The host (`run.py`)

What an app writes, and nothing more:

- **The read model.** The ledger is an in-memory SQLite. The panel is a separate SQLite
  file, seeded once and ATTACHed read-only beneath it, so a write to it raises.
- **`pool_domains`.** A pool worker calls it once, in its own process. A worker never
  sees the host's read model, only its own read-only connection to the panel.
- **The engine.** `TopologyEngine(data, [wiring.build_domain(resources)], placement,
  pool_domains=..., commit=session.commit)`, then `submit` and `run`. Without a placement
  it is the Simple Engine, unit for unit.

## What's committed

The feature-file, the authored `def/` schemas, `libconfig.yaml`, `topology.yaml`, every
element body under `lib/python-uv/<kind>/<name>/src/`, and `run.py`. Everything else is
generated: `gen_def/`, `gen_int/`, each element's `pyproject.toml`, `wiring/`, the
vendored `dizzy-kit/`, `dizzy-engine/` and `dizzy-topology-engine/`, `placement.json` and
`uv.lock`. `generate libraries` keeps a body that already exists.

Ported from DIZZY's `engine-topology-v2` branch, where it ran on the old `LaneExecutor`.
