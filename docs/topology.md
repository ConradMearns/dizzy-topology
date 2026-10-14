# Deployment topology — design notes

!!! warning "Experimental"
    This describes work on the `engine-topology` branch. The single-machine half
    ships: `validate_topology` runs inside `dizzy generate wiring` and fails the
    build, the wiring emits a placement table and the two builders that bind it,
    and `dizzy.engine.st.executor.LaneExecutor` serves it. The multi-node half is
    not deployed: `engine/groups.py` implements a group worker and
    `engine/cursors.py` durable cursor stores, but no wiring emits one package per
    group, nothing constructs either of them, and the only transport is the
    in-process doorbell. The open question at the bottom is open, and it is
    deliberately not in the site nav.

A feature-file says what a system *is*. It does not say where any of it runs, and
it should not — which elements share a process is a property of a deployment, not
of a design. Today that property is neither: it is baked into hand-written
wiring, so changing it is a code change and testing it is impossible.

The goal is one sentence: **the same feature-file, deployed at different
topologies per environment, with no code change.**

## The unit is a consumer group

A **consumer group** is a set of elements sharing one cursor over the log. That
is what `topology.yaml` names.

The distinction that matters, and the one most likely to be got wrong:

- **Worker count** is a *scaling* decision. Two workers on one group is
  operations, and safe to change without re-reasoning about correctness.
- **Group count** is a *semantic* decision. Two groups advance independently and
  can observe each other's effects out of order.

So groups are declared in a reviewed artifact and validated against the
feature-file at generation time. Worker counts are a number an operator turns.

## Two consumers, two pressures

The design has to serve both of these, and they are not the same shape.

| | **Isolation** (labthing) | **Fan-out** (DAG processing) |
|---|---|---|
| Shape | ~124 elements, one process, one global lock | 1 command → 10 → 100 → 1000 artifacts |
| The pain | A 40s paid model call freezes every write | Organising workers; finding the one failure in a thousand |
| Wants from topology | Split slow paid work off the lock | Wide worker pools, backpressure, per-unit error tracking |
| Cost sensitivity | Process count — self-hosted, ~50-100MB each | Throughput; process count is elastic |
| Must not lose | A 429-test suite that finishes in ~17s in-process | Any unit of work, ever |

Both are expressible in the current draft, which is the main evidence the schema
is not just labthing's problem in YAML. Both are shown here as first drafted, and
rule 6 below costs each of them an edit the validator now insists on — left
standing rather than quietly repaired here, because what the edit costs is the
argument:

```yaml title="isolation"
groups:
  - name: fast          # holds the lock; must stay quick
    include: [{kind: projections}, {kind: policies}]
  - name: paid          # 40s turns block nothing else
    include: [{kind: procedures, names: ["*_completion", "summarize_*"]}]
    executor: pool      # a background lane, off the lock entirely
    workers: 4
    retry: {max_attempts: 3, backoff: exponential, dead_letter: true}
  - name: default
    catch_all: true
priority:               # command TYPES, not chains
  interactive: [send_notification, "approve_*"]
```

```yaml title="fan-out"
groups:
  - name: expand        # the 10-to-100-to-1000 tier
    include: [{kind: procedures, names: ["expand_*", "derive_*"]}]
    workers: 32
    max_in_flight: 64
  - name: ledger        # one model's projections, never split
    include: [{model: artifact_ledger}, {kind: policies}]
  - name: default
    catch_all: true
```

Both examples scale a group of procedures, and `dizzy generate wiring` accepts that as
long as none of those procedures declares a `queries:` — see rule 3, which asks that same
question of every element a scaled group holds, policies included. The paid completions
and the `expand_*` tier both read nothing, so both scale as written. Concurrency means
both knobs: one worker holding 64 units in flight runs 64 units at once exactly as 64
workers do, so the gate reads `max(workers, max_in_flight)` and names whichever field the
author wrote. On the shipped executor those numbers are a declaration before they are a
width: `workers:` sizes a lane's process pool, and only a `pool` lane has one — so the
fan-out tier as drafted, an inline group with `workers: 32`, is accepted and then served
one command at a time on the calling thread. Fan-out is the multi-node half's problem.

## Two latencies, two knobs

`executor` and `priority` are the whole of the single-machine placement vocabulary,
and they exist because "the command was slow to start" is two unrelated problems that
each need their own fix:

- **Queue position.** An urgent command sits behind two hundred batch commands.
  `priority` fixes this: an interactive command is claimed before any batch command
  waiting on the same lane.
- **Head of line.** A forty-second procedure is *already running*, so the queue is not
  consulted at all until it finishes. Priority cannot help — there is nothing to
  reorder. Only `executor: pool` fixes this, by moving that procedure to a background
  lane served by a spawned process pool, so the main lane keeps claiming.

Neither knob solves the other's problem, which is why there are two of them rather
than one number. Both are built; *The single-machine executor* below walks what the
built ones do and do not promise.

**Priority has exactly two levels, `interactive` and `batch`, and batch is the
default.** Not an integer: a numeric priority has no semantics anybody can state
without running the scheduler, and every such field ends in teams bidding their own
work up. Two levels have a one-sentence definition, and that sentence is testable.

**Priority keys on command names, and does not travel along the causal chain.** It is
a property of the command *type*. A policy reacting to an interactive command's event
dispatches whatever the command type it names is declared to be — inheritance would
make latency depend on the route a command arrived by, which is precisely the thing a
topology exists to state rather than to discover under load. Note the name-space: the
`include` selectors above match *procedure* names, `priority` matches the `command:`
values those procedures consume, and in a feature-file where procedure `open_batch`
handles command `start_batch` the difference is visible. Naming the wrong one is a
generation error rather than a silent no-op.

**Pool eligibility is not a new question.** A background lane runs while the main lane
runs, so `executor: pool` is a concurrency claim exactly as `workers: 4` is, and it
goes through the same gate: rule 1 refuses a pool group holding a projection, rule 3
refuses one holding any element that declares `queries:`. What an element *reads* is
what decides whether it may leave the lock, and there is deliberately no second answer.
That cuts both ways on the recipes shape, where the two slowest procedures
(`open_batch`, `run_batch`) are exactly the two that query — "slow" and "forkable" are
not the same predicate, and the schema will not let you pretend they are.

The default topology — no `topology.yaml` at all — is one inline group with every
command batch, which is byte-identically the behaviour of a build from before any of
this existed. `dizzy generate wiring` emits no placement table for it.

Two schema decisions came directly out of writing those:

**Selectors, not enumeration.** 124 elements must not mean 124 lines. Selectors
match by kind, by name glob, or by model — and `{model: artifact_ledger}` is the
convenient *and* correct way to satisfy the rule that one model's projections may
never be split.

**`catch_all`.** The first draft made "an element claimed by two groups" a
generation error, which is right, but it also made a trailing `default` group
illegal — and without one, adding an element to the feature-file either leaves it
silently unplaced or forces every topology to be edited in lockstep with the
design. Exactly one group may be `catch_all`; overlap between *explicit*
selectors is still an error.

## What generation does

`dizzy generate wiring` already turns the feature-file into per-element bindings.
Topology makes it take a second input. Today that second input changes exactly one
block of exactly one wiring package: the placement table. One wiring is emitted, at one
path, whatever the topology says, and the `build_engine` inside it still registers every
declared projection, policy and procedure with no group filter at all. Emitting one
wiring **per group**, each engine registering only what its group claims, is the half
that is not built.

The six placement rules are all statically checkable against the feature-file, so
they are generation errors, not runtime races. Make the bad state
unrepresentable, not detectable. All six are enforced by `validate_topology`,
each against a second, naive implementation the property suite runs the real one
against. Five of them have a rejection test of their own; rule 2 is a licence and
so has acceptance tests instead — there is nothing for it to reject:

1. **Projections for one model share a group, and that group runs serially.** A model
   is a fold in log order; scatter its projections and the fold interleaves — silently,
   and only under load. This is the rule a plausible-looking config is most likely to
   break. N workers in one group, or one worker holding N units in flight, interleave
   that same fold exactly as two groups do, so the rule is about concurrency and not
   merely about group count: a group holding any projection is pinned to one lane.
2. **Policies partition freely when they read nothing.** They react and dispatch;
   commands dedupe on deterministic ids, so observing events in another group's order
   is harmless. That argument is about event order and nothing else, so it stops where
   the policy starts reading: a policy declaring `queries:` decides on a model exactly
   as a procedure does, and rule 3 gates it identically.
3. **An element partitions when it reads nothing.** A procedure or policy declaring no
   `queries:` computes its output from the command or event alone — no concurrent peer
   can reach it, so it parallelises freely. One that *does* query is a read-modify-write
   over a model a peer may be changing underneath it; with no aggregate key in the
   feature-file there is nothing to serialise it by, so a group holding one runs
   serially. The discriminator is the `queries:` list, never the kind of the element
   that declares it. Per-aggregate keys are what would relax that second case, and until
   they exist a reader is the only thing that pins a group to one lane.
4. **Queries are not in groups.** They are reads over a model. Placement is a
   question about the read substrate, not the log — which is why `ElementKind`
   has no `queries` value. A selector reaching a query name is refused, with the one
   exception the name-space forces: nothing makes names unique across kinds, so an
   exact `names:` entry that is *also* a declared procedure, policy or projection
   unambiguously claims that element and is accepted. A glob gets no such exemption —
   a pattern that sweeps up a query name reads as claiming the query, and is refused
   even when it matches elements too — and neither does an exact name that resolves to
   no element at all.
5. **Every element is claimed by exactly one group.** Two claims and the element
   belongs to nobody — neither group's worker can assume it ran, and the rule-1
   fold order stops meaning anything. No claim and it never runs at all, which
   is the failure a `catch_all` group exists to make impossible; at most one
   group may be that group. This is the rule the shipped validator's messages
   name, and it is checked before any of the others, because a doubly claimed
   element would otherwise cascade a rule-1 error on top of the message naming
   its real problem.
6. **A reader shares a group with what it reads.** An element declaring `queries:`
   decides *on* a model, and what the log records is the decision, never the model. A
   reader whose group trails the group folding a model it reads does not merely see
   stale data — it emits a *different event*, so two deployments' logs diverge in
   content rather than converging late, and no refold repairs a log that is short an
   entry. So the group claiming an element must also claim every projection folding
   every model that element's queries read. Discriminated exactly as rule 3
   discriminates, and the two are the same fact on two axes: rule 3 says a reader
   forbids concurrency *within* its group, this one says it forbids *distance* from
   the model it reads. An element that reads nothing is untouched by both, which is
   why the paid `*_completion` tier and the 32-wide `expand_*` tier survive unchanged.
   Two things it is silent about, deliberately. A query stating no `model:` reads
   nothing the log folds, and a model no projection folds sits in no group at all —
   neither gives a reader a group to share, so neither constrains where it goes.
   That is where rule 6 and rule 3 stop agreeing: an element whose every query is
   model-less or unfolded still pins its group to one lane and may still be placed
   anywhere.

Co-membership is the only cure for that divergence that stays static, and it works
because inside one group there is one cursor and one lane — rules 1 and 3 already pin
any group holding a projection or a reader to a single lane — and a group worker folds
and commits before running the elements it claims at that position, which is the
guarantee `Engine.ingest` enforces today with four sequential statements and a
group worker must inherit.

The closure rule 6 induces does not chain, and that is structural rather than lucky: a
projection declares no `queries:`, so no reader is reachable from another reader through
a model. The constraint graph is bipartite — readers on one side, model cohorts on the
other — and two cohorts merge only where one element's own `queries:` list spans both.
The rule is also deliberately silent about *writes*. An element's emissions go to the
log, which is the truth and is ordered; a projection folding them late is latency, not
divergence. Pinning emitters to the folds of what they emit is what would make the
closure transitive: on `examples/recipes` it unions all four models and drags every
`record_*` procedure in via its own catalog fold, collapsing all 20 elements into one
group. The silence about writes is load-bearing, not an omission. Queries stay exempt
(rule 4) for the same reason the rule stops at reads: a stale read served to a person is
latency, a stale read served to a *decision* is divergence, and only the second is the
log's problem.

What rule 6 costs is not throughput — the set it constrains is exactly the set rule 3
had already pinned to one lane — but the freedom to put a decision anywhere other than
with the folds it reads, and on a read-heavy feature that is a real cost paid by the
groups least able to afford it. On `examples/recipes` the readers are `open_batch`
{catalog, inventory}, `run_batch` {batches, catalog, inventory} and
`advance_ready_batches` {batches}, which fuses 10 projections and all 3 readers into one
13-element group. That leaves seven elements in six further placements — the five
`record_*` procedures are free, but `generation_graph` and `derivation_graph` both fold
`provenance` and so are one unit by rule 1 — for a maximum of seven groups, not one.
Thin, and a property of recipes (a chained workflow where nearly every decision reads
shared state) rather than of the rule.

**Both worked topologies above pay for it, and the isolation one is illegal as
written** — `dizzy generate wiring` refuses it, naming the reader and the group
folding what it reads. `fast` claims *every* projection, so every querying procedure
the `default` catch-all sweeps up — `open_batch` and `run_batch` in the recipes shape —
breaks rule 6, and the only legal repair is to move them onto `fast`, the group whose
stated job is to hold the lock and stay quick — `include: [{kind: projections},
{kind: policies}, {kind: procedures, names: [open_batch, run_batch]}]`. What
survives is the thing that topology was bought for, because the `paid` tier reads
nothing and still comes off the lock; what rule 6 takes away is splitting
*read-decisions* off it. The fan-out topology pays the
same way and not, as an earlier draft of this section said, by narrowing one selector:
`{model: artifact_ledger}` now determines where every reader of `artifact_ledger` lives,
wherever the catch-all would otherwise have put it, so the ledger group and its readers
must be co-declared. That a `{model: X}` selector reaches beyond X's projections is the
part of rule 6 most likely to surprise an author.

Failure names the element and the rule it broke. Three smaller conditions are
generation errors for the same reason the six rules are — a deployment that cannot
be what it says it is should not be representable:

- A group that can claim nothing — no `include` selectors and no `catch_all` — is a
  typo with a runtime cost: a process consuming a cursor and running nothing.
- A group that claims no element may not ask for concurrency. There is nothing in it
  to spread.
- An `include` selector must claim at least one element. An empty `names: []` claims
  none, and is reported as such rather than widening to whatever its other fields
  allow.

## The single-machine executor

Both knobs are built, and on one machine they are the whole story.
`dizzy.engine.st.executor.LaneExecutor` reads the generated `LANES` table verbatim —
every non-`pool` lane served on the calling thread, and one background thread per `pool`
lane, each of which builds its process pool on that lane's first dispatch (a lane that
never claims anything spawns nothing) — and `st.DurableCommandQueue` holds the rows,
with the lane and the priority stored as columns decided once at enqueue. Neither of
them knows a command name. A topology-free build constructs no pool and starts no
thread, which is asserted rather than assumed.

### Two latencies, two mechanisms

The split from *Two latencies, two knobs*, restated against the code that now
implements it, because the two mechanisms are unrelated and get conflated constantly:

- **Queue-position latency.** An urgent command is enqueued behind two hundred batch
  commands. The fix is the two-level priority in the *claim order*: the claim is
  `... WHERE status='queued' AND lane=? ORDER BY priority DESC, id LIMIT 1`, so an
  interactive row is served ahead of every batch row waiting on its lane, and within a
  level the order is insertion order. `dizzy/tests/engine/test_st_isolation.py` asserts
  that shape directly — two hundred batch rows queued ahead of one interactive command,
  and the first `pump()` runs the interactive one with all two hundred still queued.
- **Head-of-line latency.** A forty-second procedure is *already running*, so the queue
  is not consulted at all until it returns. There is nothing to reorder, so priority is
  irrelevant here. The fix is `executor: pool`: a lane thread claims that procedure and
  dispatches it to a spawned process pool, and the main lane goes straight back to
  claiming. The same test file holds a pool procedure blocked indefinitely and asserts
  that the main lane still folds an interactive command inside the budget it names —
  next to the control, where the same block on a single inline lane folds nothing at
  all for as long as it is held.

Neither mechanism does the other's job. Priority cannot touch a command that is already
executing: a running command holds its lane, and there is no preemption, no ageing and
no fairness rule anywhere in the queue. A pool lane says nothing about the order rows
are claimed in. A system with both a slow procedure and an urgent command therefore
needs both, and the two are asserted together in one test — two hundred batch rows
queued *and* a pool procedure blocked, with the interactive command still landing
inside the budget and nothing batch running in between.

What that budget is worth is worth stating precisely. It is a deadline a test asserts
over the main lane's own work while an in-process double holds the block, and it is the
harsher stub, because a real child blocks in another process entirely. Nothing measures
interactive latency across a real process boundary: the spawning tests assert
correctness and a shutdown bound, never how long an urgent command waited. Take the
property, not a figure.

### One writer

The invariant the pool lane is built around: **only the main lane touches the log and
the read models.** A pool child runs a procedure *body* and returns the events it
emitted as data. "Main lane" is whichever thread calls `pump`, `drain` or `stop`; that
it is one thread is a contract on the host, which the executor documents and does not
check.

Exactly one payload crosses the process boundary each way. Out: the host-app spec, a
snapshot of `sys.path`, the lane name, and the command. Back: a single frozen
`ProcedureResult` carrying the emitted events, an error string and a formatted traceback
(never an exception object), the child's pid and a duration. Only a procedure's input
and its output cross; the store does not, and the read-model session does not. The job
the child runs calls `refresh()` and `run_procedure`, and `run_procedure` by
construction touches no store on the write side. The parent then ingests the returned
events on the main lane, unchanged — they are the generated pydantic instances
themselves, pickled by class reference.

The child is fenced on both sides of that seam. Its `ShellServices` hands it a command
queue whose `put` raises and reports itself empty, no publish and no observer: it cannot
dispatch a command and emits no telemetry of its own, so the parent emits the
procedure's bus record instead, stamped with the lane and the child's pid. On the write
side the fence is which runtime it builds. The generated wiring binds
`build_procedure_runtime`, which passes `store=None`, so that child opens no event store
at all — asserted by probing the child's own fd table from inside it. A host that
supplies no procedure runtime falls back to the full one, and *that* child does open the
host's log; what is proven of it is only that it cannot **write** it, because
`EventStore` defaults `read_only` to "did multiprocessing start this process" and the
sqlite connection is then opened `mode=ro` and runs no schema DDL. State the invariant
in that shape: a child holding a procedure-only runtime opens no log, a fallback child
opens it read-only. Read-only is the mitigation; the empty fd table is the cure. And the
refusal is real for *dispatch* and only incidental for *append* — nothing in the
executor stops a procedure runner from calling `ingest` itself, it simply has no store
to succeed against.

One more check sits in front of the ingest. Before appending a completion's events the
parent rejects any event whose class the store does not declare — when the store carries
a class map at all; one that does not opts the check out — discarding the batch
and erroring the row, because `append` never consults the class map and a foreign event
would otherwise surface much later, at reconstruct or rebuild time.

### What each lane class guarantees

`test_conformance.py` pins one guarantee per lane class as a literal table and asserts
both directions, so a convergent lane that quietly became sequential fails it exactly as
a sequential one that slipped would.

| Lane class | Guarantee | What is asserted |
|---|---|---|
| main, inline | **sequential** | One lane, one process, claim order — `st`'s defined semantics, one legal interleaving. An executor with no pool lanes reproduces the hand-written sequential run's stream *and* its read models exactly. |
| pool | **convergent** | Background procedures fold in *completion* order, which the main lane does not control, so the log need not match the sequential one while the read models are identical. Asserted with a deterministic pool double that completes in reverse: the stream *inequality* is asserted, not merely tolerated, alongside multiset equality — the same events, a different order. |
| pool with `ordered=True` | **sequential, inside that one lane** | A reorder buffer holds each completion under its dispatch seq and releases the contiguous prefix, so the lane folds in the order it claimed; opposite completion orders give the same store. |

The third row is real, and narrow, and the narrowness is the point. `ordered=True`
restores *one pool lane's own* dispatch order and nothing more. It is established for a
single pool lane holding every command and drained to quiescence; nothing orders two
pool lanes against each other, nothing orders a pool lane against the main lane, and
since a pool procedure runs concurrently with the main lane, whether its events land
before or after the inline command in flight is a race every time. A topology with an
inline lane and a pool lane live together is asserted to converge in its read models,
and no stream property is claimed for it at all. The buffer also costs something, though
less than it looks: a completion that finishes early is held until its predecessor
arrives, so its events reach the log and the read models later than they could have.
What waits is the *fold*, not the lane — the slot is released and the lane goes on
claiming and dispatching — so this is delayed visibility inside the background lane
rather than head-of-line blocking reintroduced. That is the trade, stated rather than
hidden.

### Priority is a property of the command type

Priority keys on the command *type* and does not travel the causal chain. In the shipped
generated wiring it is a static lookup: `COMMAND_PLACEMENT` maps a command class name to
`(lane, priority)`, baked at generation time from the topology's `priority.interactive`
globs. So a policy reacting to an urgent event and dispatching a batch command
dispatches a batch command, at batch priority, however urgent its cause. Making a whole
chain urgent means naming *every* command in that chain under `priority.interactive` —
the topology stating the latency it wants, rather than a runtime discovering it from the
route a command arrived by.

That is also what keeps `st` deterministic under priority: because the level is a pure
function of the type, two runs claim in the same order and produce the same stream and
the same models, which is asserted. The queue underneath is more permissive than the
generator above it — it stores whatever integer the injected callable returns for the
command *instance*, and `ORDER BY priority DESC` would honour any value, with no `CHECK`
constraint and no clamp. Two levels, and priority as a function of the type, are
conventions the generator keeps, not invariants the schema enforces.

Priority is fixed at enqueue. Nothing updates the column afterwards, a retried row keeps
its original level and its original position, and there is no ageing, so a level is
exhausted before the next is looked at and batch rows can be deferred indefinitely under
an open workload. That is the deliberate policy — ageing would make claim order a
function of wall clock and cost `st` its determinism — and the remedy on offer is a
second lane, never a third level.

### The event queue is never reordered

Priority is a command-queue concept and must stay one. A command queue is a work list,
and reordering a work list changes only which unit of work runs next. The log is not a
work list: it is the truth, and a projection is a fold over it *in order*. Reorder the
events and you reorder the fold, and a fold taken in a different order is not a late
model, it is a different one. There is no priority column on the log, no lane on an
event, and there must never be either. The only ordering knobs the engine has are which
command is claimed next and, inside one pool lane, which completion is ingested next;
the positions in the log are never among them.

## What a group worker does

None of this is *deployed*. `engine/groups.py` implements the worker described below and
`engine/cursors.py` the cursor it holds, but no shell and no generated wiring constructs
either — `st` and `mp` still drain through `Engine.ingest`, and the module says so itself.
The single-machine executor above is a `LaneExecutor` over one engine in one process. What
follows is the design the multi-node half would have to satisfy.

A group is a unit of *serialization*, not a unit of intake. It has one lane of
execution and up to two intakes.

**Event intake — a cursor.** A group claiming projections or policies holds one
cursor over the log:

```
read cursor  →  fetch log entries after it  →  fold this group's projections  →
commit  →  run this group's policies  →  advance cursor
```

That is `Engine.ingest`'s body with the append taken out of it, in the
same order, so the four sequential statements survive the split verbatim.

**Command intake — a lane.** A group claiming procedures holds one lane of the
durable command queue, named after the group. No cursor is involved: a command
is not in the log, so there is no position to hold. A policy's dispatch is
routed to the lane of the group claiming that command's procedure, from a table the
generator computes out of the topology — so nothing in `engine/` learns a command
name. That table is `COMMAND_PLACEMENT` in the generated wiring: a mapping from
`type(command).__name__` to `(lane, priority)`, beside a `LANES` mapping giving each
lane its executor and worker count. It is generated **data**, never a hand-written
callable, because a callable is a second place the deployment is described and it
drifts from `topology.yaml` in silence.

What consumes it is `dizzy.engine.st.executor.LaneExecutor`, which reads `LANES`
verbatim: one inline lane served in the calling thread, one background thread and one
spawned process pool per `pool` lane, all over the single durable queue, with the log
still written by exactly one thread. The last hop is generated too: beside the table the
wiring emits `build_command_queue()` and `build_lane_executor(queue, engine)`, which
bind `command_lane`/`command_priority` and `LANES` respectively. So a host writes the
lifecycle — start, pump, stop — and never restates the deployment; the principle that
placement is never hand-written does not stop at the table. It does stop at the host:
nothing in the shipped engine or either shell calls those two builders, so a host opts
into them by hand, and there is no checked-in host that does.

**Most groups are both**, because rule 6 fuses readers with the folds they read
and readers are mostly procedures. A hybrid worker's cycle is **fold to tail →
claim one command → fold to tail → run it → fold to tail**. A fold is what
carries the central guarantee across a group split: a policy-dispatched command
is causally after the append of the event that triggered it, and positions are
local insertion order, so a worker whose fold covers the command's arrival in
the lane has necessarily already folded that command's triggering event. It
needs no coordination, but it does need the fold to be taken *after* the claim — which
is why `GroupWorker`'s command intake is two callables, `claim` and `execute`,
rather than one that does both. A claim may block for as long as it likes; the
fold between the two picks up whatever was appended during the wait, so the
event that dispatched the very command being claimed is in the model that
command reads. Fusing them back together is exactly the defect the pair exists
to prevent, and the fold is taken inside the worker rather than asked of the
host so that forgetting it is not an option a host has. The trailing fold is
for `run_command`'s own post-condition — that projections have folded by the
time it returns — which every host that runs a command and then renders
depends on.

The guarantee that survives the split is not quite the one `ingest`
states. It is: **the read model commits before the dispatched command runs.**
Inside one group those are the same sentence; across groups only the second is
available, and it is the half anything downstream actually needed. What is given
up with it: a hybrid group folds to *tail*, so it also folds entries another
group appended that a sequential run would have ordered later, and a procedure
branching on that reads a model further ahead than `st` would have handed it.
Models still converge — that is confluence — but two topologies' logs need not
agree entry for entry, which is the residual the last section names.

The expectation that goes with rule 6 is narrower than an earlier draft of this
section claimed, and the correction came from modelling the worker rather than from
running it. What rule 6 buys is the guarantee stated just above — the read model has
committed through the command's *cause* before the procedure runs — and that holds
with the worker exactly as written. What it does not buy is `st`'s guarantee. The
worker's cycle folds to tail, claims, folds to tail, then runs, and nothing holds a
lock between that second fold and the procedure's first read; another group may append
in the gap. So a rule-6-legal deployment can still decide on a model that is behind
the log, and two rule-6-legal decisions can each miss the other with no serial order
explaining both. "Byte-identical models, entry for entry" is therefore false for the
worker as it stands, and it is a claim to stop making until the claim→execute window
is closed — a lock, or a re-check of the tail after the claim, which is a design
decision on its own evidence and not a consequence of rule 6.

Violating rule 6 loses even the weak guarantee: the reader decides on a model that has
not folded the command's own cause. Reading an empty model and killing a cascade is one
way that shows up, and the cheapest to demonstrate, but the general failure is a stale
read reaching a decision, not an empty one. The property test named at the end of this
document is what would put numbers on any of it. The single-machine claims above have a
harness — `dizzy/tests/engine/test_st_isolation.py` asserts the two latency properties
and the isolation invariant with real deadlines. Nothing about the *group worker* has
been measured, and none of it should be quoted until it has.

The cost is a fairness policy the earlier draft never contemplated. A hybrid
worker chooses between draining its lane and advancing its cursor;
commands-first starves folding under replication load, folds-first starves
commands under fan-out. Whatever that rule is, it is part of the defined
semantics the way the shell already is.

Two properties make the loop safe to distribute:

**The transport is a doorbell only.** It carries "wake up, there is work" and
nothing else. Correctness comes from the cursor, so a dropped notification costs
latency and never costs work. That is what makes `inprocess`, `zmq-ipc`, `nats`
and `postgres-notify` genuinely interchangeable — and what keeps them a config
value rather than an architecture.

**Cursors are durable but disposable.** Persisted so a boot is `snapshot + tail`
rather than `replay(all)`, because a log only grows. But losing one must be
*safe*, not merely recoverable: commands carry deterministic ids and procedures
guard, so replaying from zero re-dispatches everything and every one no-ops. That
property is what keeps a cursor a performance checkpoint instead of a second
source of truth, and it needs a test, not a paragraph.

What a cursor may *not* be is a position in canonical order. `DagStore.iterate` is Kahn
over the whole event set tiebroken by content-hash id, so the order is a pure function
of that set and a replicated event carrying old parents splices into the *middle* of it:
one peer fact forked off an early entry renumbers most of the positions after it.
`Envelope.seq` is the per-call enumeration index, nothing stores it, and
`add_replicated` never even sets it. A cursor is therefore either a **frontier** — a set
of event ids whose causal past has been folded, resumed by the walk-back-until-known
idiom `dagstore/sync.py` and `replicate.pull` already implement — or a **local insertion
watermark**, which is prefix-stable, always a valid causal order, and the only one of
the two that *seeks* — the frontier has to rebuild canonical order before it can yield
anything. The watermark is node-local and must never be compared between peers; the
frontier is the portable one. Two store defects block a tailing cursor and are named
here so they are not discovered later: `DagStore` is the one durable component in the
engine that does not set `journal_mode=WAL`, and `iterate`'s two Kahn setup statements
are separately autocommitted, so a foreign commit landing between them misclassifies a
child as a root and emits it before its parent. Nothing tails the log today, which is
the only reason that is latent.


### What `workers: N` buys, per kind of group

`save` is monotone, which makes a cursor one lane's watermark and *not* a thing
two workers may split: the leader's save covers a prefix the laggard never
committed, and the crash loses that work silently. So the knob answers three
different ways, and conflating them is how it becomes a lie.

- **A group holding a projection or any reader.** Refused, as today — rules 1
  and 3 pin it to one lane and `_effective_concurrency` already errors.
- **A procedure-only group.** N workers on one lane, and genuinely just
  operations — with one prerequisite. `claim` is a `SELECT` then an `UPDATE`
  guarded by a `threading.Condition`, which serializes one *process*, not one
  machine: two connections on one file claim the same row. Until it is a
  conditional `UPDATE ... WHERE status='queued'` with a rowcount retry,
  `workers: 2` runs concurrently what the operator was told was merely retried.
- **An event-only group of non-reading policies.** N cursors, not one. Worker
  *k* of *N* owns `<group>#k/N`, reads the whole stream from its own position,
  acts on the entries whose content hash mod N is *k*, and saves on every entry
  whether it acted or skipped — a skipped entry is done, by ownership. Each
  watermark is then honest and a crash re-delivers one shard's tail. The price
  is that changing N invalidates every assignment: the only safe reshard is to
  rewind all N cursors to the lowest and replay, which is safe only because
  those policies read nothing and commands dedupe on deterministic ids — the
  property this document now rests on twice and still has no test for.

Rejected: one cursor, N workers, disjoint batches. That is the at-most-once hole
`save`'s monotonicity hides rather than fixes.

## Where a command lives — decided, and what was deferred

Two answers were built and weighed against each other. This is the one that
lands; the reasoning matters more than the verdict, because the other one may
still be right later.

**Decided: a command's durable home stays the queue, and a procedure group has
no cursor.** A group gets a cursor if and only if it claims something that
consumes the log. Procedures consume commands, commands stay in
`st.DurableCommandQueue`, one lane per group. The question "what does a
procedure group's cursor read?" is answered by refusing its premise rather than
by inventing a reading.

That narrows the crash window without a second substrate. A cursor-driven policy
group saves its cursor only after the `put` has committed, so a crash between an
event's append and the enqueue leaves the cursor unadvanced, the policy re-runs
on the same entry, and the command is re-dispatched. For the reactivity loop the
window that made "add a durable queue" the wrong answer is shut.

What stays open, said plainly:

- **Externally-originated commands.** An API call that dies before its `put`
  commits is gone, with no log entry to re-derive it; only a client retry
  recovers it. The log's genesis still is not always an event.
- **A new hazard, from the cursor rather than despite it.** `command_queue.db`
  is a second file beside the event store. Lose it and every queued, unclaimed
  command vanishes while the policy group's cursor has already advanced past the
  events that produced them — a durable record asserting work that never
  happened. Today `st` has the same file and no cursor to be wrong about, so a
  rebuild recovers. Under this design, dropping that file must be paired with
  rewinding every policy group's cursor. An operational coupling between two
  stores is exactly the smell this section opened by naming: it has been moved
  and made quieter, not removed.

**Deferred: recording command requests in the log.** It is still the better
answer, and it was built far enough to say precisely why it is not this
increment's. Shaped as a parentless entry in a reserved type namespace —
edgeless, so it never enters ancestry, never replicates, and never perturbs
canonical order — it passes the conformance suite unmodified, and "requests not
yet followed by their outcome" becomes an ordinary read model, cheap to rebuild from
zero. Three things have to be true first:

- **The back-reference is inside the hash.** Naming the request in the event's
  wrapper changes every caused event's id, because the wrapper *is* the hashed
  payload. That is a hash-format decision, not a storage one, and it belongs in
  its own change.
- **`heads()` has to learn a type predicate**, or parentless requests become
  permanent heads and the next append drags them back into ancestry. Editing the
  correctness kernel is not a side effect of a topology increment. `iterate`,
  `__len__` and `seq` need the same filter, and every omission is silent in a
  different way.
- **`append` is quadratic** — `heads()` is an unindexed anti-join run inside
  every append, so doubling the entries per unit of work more than doubles the
  ingest cost. A maintained frontier table flattens it, is worth doing on its
  own, and is a prerequisite here.

One variant must not be revived: parenting each entry on the entry its element
consumed. It turns the chain into a branching DAG, canonical order then
tiebreaks concurrent siblings on a hash containing `ingested_at`, and two runs
of the same script iterate in different orders — which fails
`test_the_sequential_shell_is_deterministic` and, worse, breaks the convergence
invariant the DAG exists to provide. Dropping `ingested_at` from the hash
repairs it, at the cost of the append time no longer being part of the
hash-protected fact. That is a decision on its own evidence, not a step in this
one.

Two things named earlier are still not designed:

- **Backfill for a new policy.** A policy added today cannot sensibly default its
  cursor to zero (it re-dispatches all history) or to head (it silently skips
  work). This wants to be a declared property of the policy in the *feature-file*
  — `backfill: none | from-genesis | from-heads` — decided when the policy is
  authored, not when it is deployed.
- **Aggregate keys.** Rules 3 and 6 both split elements on `queries:` — a sound
  discriminator, but a blunt one. A procedure reading a model it never writes is
  safe to parallelise and is refused anyway; two reading disjoint aggregates likewise.
  An aggregate key in the feature-file is what would let the validator say yes to
  those, and it is the difference between "reads nothing" and "reads nothing *this
  worker is racing*". It loosens both rules identically — two readers over disjoint keys
  need not share a group with each other, and a reader may sit apart from a fold it
  provably never races — so rule 6 should be written as a predicate an aggregate key
  later *narrows*, not one it replaces.

## The open question is multi-node

One question is left, and it is worth being exact about its scope, because it is easy to
read the rest of this document as blocked on it. It is not. **The single-machine
deployment is decided and built.** A `topology.yaml`, a validated placement table, an
inline lane and a pool lane over one durable queue and one writer — that whole path
ships, has its guarantee pinned per lane class, and needs nothing below to be answered.
(The executor takes any number of pool lanes; every test topology has exactly one, so
nothing is established about ordering or fairness *between* two of them.) Everything
that follows is about what happens when a topology spans *more than one node*: several
group workers, several cursors, several processes appending to logs that must agree. A
team that wants slow work off the lock, or an urgent command served ahead of a batch, is
not waiting on any of it.

The multi-node question is: does this subsume `st` and `mp`? Possibly, and that is worth
deciding deliberately rather than discovering.

`st` falls out as a topology: one group, `inprocess` transport, `memory` cursors,
one worker. It is a *hybrid* group — it claims procedures, so it holds a lane as
well as a cursor, and both intakes are served by the one thread. That is the
degenerate case of the loop above and not a special one, which is the whole
reason to believe it: `st` stops being a shell and becomes a default.

`mp` is a harder case, because its model differs rather than specialises. Its
unit of distribution is the *command*, routed at dispatch by the app's `routes()`
into broker pools, and its correctness rests on the broker's at-least-once
delivery. This design routes by *cursor over a log* and treats delivery as
advisory. Both can be true at once, but maintaining two distributed shells with
different correctness stories has a real cost — the conformance suite has to
assert both, forever.

The guarantee is the thing to compare. `test_conformance.py` currently asserts
that `st` is sequentially consistent and that `mp` deliberately is not, with a
test whose whole job is to stop `mp` being quietly promoted. The topology claim —
*same log, same feature-file, any valid topology, byte-identical models* — is
stronger than what `mp` promises. If it holds, this is not `mp` with a config
file; it is `st`'s observable guarantee at `mp`'s scale, bought by constraining
placement instead of trusting a broker.

Rule 6 strengthens that claim without establishing it. It removes the failure mode
that makes two logs differ in *content* — a decision reached against a stale model —
which is the half no refold can repair. The residual gap is not about reads at all:
`DagStore.append` names *the current local heads* as parents, so two groups appending
concurrently mint different parent tuples, hence different content-hash ids, hence a
different DAG. Since `iterate` is a deterministic function of the event set, the
claim's second half — given the same DAG, every topology folds byte-identical models
— is already true and is the convergence invariant. The unfinished half is the first:
parents must become a function of the entry the element consumed rather than of
whatever the frontier happened to be at append time. That is a change to `append`,
and it is the companion work to rule 6 rather than something rule 6 assumes away.

That claim is a property test, not an example: random valid topologies, random
interleavings, replay, compare. It is the first thing to write for the multi-node half,
because if it does not hold, that half does not matter — and it is the *only* thing
gated on it. The single-machine executor's guarantees are asserted already, one per lane
class, and stand whatever this test turns out to say.
