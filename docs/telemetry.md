# Telemetry

The topology engine traces itself the way the Simple Engine does (`ext/dizzy/docs/design/simple-engine.md`
§9), and adds what lanes and pools introduce. There is one trace per engine run, rooted
at `dizzy.run`, with span ids derived from the run's session and the log's ids. Every
finished span goes to `<data>/telemetry/<run>.jsonl`, and to an OTLP/HTTP collector when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set. Each lane's queue is counted too, as two up/down
counters (*Queue counters*). Telemetry is never the source of truth: spans link
to the log by ids, losing them changes no decision, and nothing reads them back.

## Seeing it

[otel-desktop-viewer](https://github.com/CtrlSpice/otel-desktop-viewer) is the sink to
start with: one binary or container, OTLP on 4317 (gRPC) and 4318 (HTTP), a waterfall UI
on 8000, and traces, metrics and logs kept in an embedded DuckDB.

```bash
docker run -d --name otel-viewer -p 4317:4317 -p 4318:4318 -p 8000:8000 \
  ghcr.io/ctrlspice/otel-desktop-viewer:latest --host 0.0.0.0 --open-browser=false
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 just assay   # then open http://localhost:8000
```

Each run is one trace, named by its root `dizzy.run`, under the service named after the
domain. The engine sends spans as they finish, in batches, so the root and each
`dizzy.pool` span (which finish at close) arrive last. A trace is whole once the engine
has closed.

## What the trace holds

```
dizzy.run                         the engine, open to close; dizzy.lanes = the placement
├─ dizzy.recover                  the refold and settle on open
├─ dizzy.pool <lane>              a pool's life: built on the lane's first claim, shut down
│  └─ dizzy.worker                with the engine, or when a worker dies (dizzy.pool.broken)
│                                 each worker's start, process start → its tables built
└─ dizzy.unit                     one command, claim → settle (a host command's)
   ├─ dizzy.procedure <name>      inline: on the host thread
   │  ── or, for a pooled unit, on the worker's clock ──
   ├─ dizzy.pool.wait             claimed → started in a worker (dizzy.pool.cold: its first job)
   ├─ dizzy.procedure <name>      in the worker (process.pid)
   ├─ dizzy.pool.handoff          finished → collected by the host thread
   ├─ dizzy.append                the block and the ack, one transaction
   ├─ dizzy.projection <name>     each fold of each appended event
   └─ dizzy.policy <name>         each reaction, and under it…
      └─ dizzy.unit               …each unit it dispatched, so a causal chain nests whole
```

On every `dizzy.unit`, inline or pooled, alongside the Simple Engine's attributes:

| attribute | meaning |
|---|---|
| `dizzy.lane`, `dizzy.executor` | where it ran: the lane, `inline` or `pool` |
| `dizzy.priority` | `interactive` or `batch` |
| `dizzy.queued_ms` | enqueued → claimed: how long it waited behind other work |
| `dizzy.recovered` | it re-ran an attempt a crash interrupted (absent otherwise) |
| `dizzy.procedure_ms` | pooled only: the procedure's time in the worker |

The pool spans carry `dizzy.pool.workers` (the lane's width), `dizzy.pool.jobs`,
`dizzy.pool.failed`, `dizzy.pool.started_workers`, and on a pool a dying worker broke,
`dizzy.pool.broken` with an error status.

What each one answers, when deciding a placement:

- **Is the main lane the bottleneck?** Compare `dizzy.queued_ms` across lanes. A pooled
  unit's `dizzy.pool.handoff` is time a finished result waited for the host thread.
- **Is the pool too narrow?** Warm `dizzy.pool.wait` time means jobs queued behind busy
  workers.
- **Is the pool worth starting?** Cold `dizzy.pool.wait` and `dizzy.worker` show what
  each worker's spawn costs: interpreter, imports, `pool_domains`. On the assay example
  that is 240–380 ms per worker, against a 40 ms procedure.

## Queue counters

Two OpenTelemetry up/down counters (an OTLP `Sum`, cumulative and not monotonic), one
series per lane, store and priority (`dizzy.lane`, `dizzy.domain`, `dizzy.priority`):

| metric | +1 | −1 |
|---|---|---|
| `dizzy.lane.queued` | a command is enqueued (by the host or a policy), or a failed one is retried | it is claimed |
| `dizzy.lane.in_flight` | it is claimed | it is settled: ack, folds, policies (inline), or its result committed (pool) |

`queued` is commands waiting for a claim, so it counts an interrupted attempt waiting to
re-run as well. On open, and when a run is interrupted by an exception, both are set
from the stores rather than moved: `queued` to every command still queued or running
there, `in_flight` to zero.

Every change is one data point, timestamped when it happened, so the series is the
exact step function rather than a sample. Points go to
`<data>/telemetry/metrics/<run>.jsonl` (`name`, `time`, `value`, `attributes`) and, when
`OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` is set, to
`/v1/metrics` in the same batches and with the same failure pause as the spans. Each
point also carries `dizzy.session`, so two runs are two series.

What they answer:

- **Which lane is the bottleneck?** A lane whose `queued` climbs while its `in_flight`
  sits at its width. On the assay example the one-wide `classify` pool holds 24 queued
  at its peak while `compute` (four wide) drains.
- **Is a pool wider than it needs to be?** `in_flight` never reaching the lane's width.
- **Is interactive work waiting?** `queued` with `dizzy.priority = interactive`.

## How it is built

`dizzy_topology_engine/telemetry.py` wraps the Simple Engine's tracer. Everything the
inherited engine traces goes through it unchanged, and it adds lane attributes to each
`dizzy.unit`. A pooled unit's span begins at its claim and ends after its settle, in a
different turn of the host loop. A worker's spans have times from another process. Both
are written by the adapter, through the tracer's own sinks and in its record format.
This is the one place the engine uses the tracer's internals (`stack`, `file`, `otlp`,
`run`). The counters' exporter is the Simple Engine's OTLP batcher, sending metrics
instead of spans.

A worker reports its times in the `Outcome` it returns: when its procedure started and
finished, its pid, and, with its first job, when its process started and finished
building its tables. Nothing about telemetry crosses to a worker, and nothing a worker
reports reaches a decision.

## Not built yet

- **More metrics.** Unit counts by outcome and duration histograms by lane would be
  derived from what spans already hold. Add them when a dashboard needs them.
- **Logs**, deliberately. Errors are on spans already (status, `dizzy.error`). A
  procedure's own logging isn't captured.
- **The profiler.** `dizzy-topology profile` still reads v2's `JsonlBusSink` records. It
  should read these spans instead, since everything it computes is in them, with lanes
  and pool waits added (`dizzy-topology-964a`).

## Limits

- **One machine's clocks.** Worker times are wall-clock (`time.time_ns`) and are compared
  with the host's. On one machine that's sound to well under a millisecond.
- **Process start time is exact only on Linux.** It's read from `/proc` (to a clock
  tick, ~10 ms). Elsewhere a worker's start is when its pool module was imported, so the
  interpreter's startup lands in `dizzy.pool.wait` instead.
- **A dead worker leaves only the error.** A job whose worker died has no procedure span,
  only the unit's error and the pool's broken status.
- **Live viewing is partial until close.** `dizzy.run` and each `dizzy.pool` span are
  sent when they finish, at close or when a pool breaks. Counter points are sent in
  batches of 512, and at the end of each `run()`.
- **A crash loses the counters' last moves,** not their truth. A process killed between
  points leaves its series where it was. The next open sets them from the stores.
- **Telemetry export is best-effort,** as in the Simple Engine. A failed send drops the
  batch and pauses export for 30 s, and never fails a unit.
