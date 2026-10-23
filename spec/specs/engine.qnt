// specs/engine.qnt — Spec 4: the topology engine (engine/src/dizzy_topology_engine),
// over an ARBITRARY feature graph and an arbitrary rule-3 placement.
//
// The topology engine is the Simple Engine (DIZZY's docs/design/simple-engine.md)
// with lanes: an inline lane served on the host thread, and a pool lane whose
// procedures run in worker processes while the host thread keeps claiming. Unlike
// st.qnt (the pre-Simple-Engine `dizzy.engine.st`), the store's transaction
// boundaries are what make it correct, so they are modelled one step each:
//
//   claim        attempt `running`, durably, before anything runs  (_claim_next)
//   append+ack   the block AND the command's `done`, ONE transaction (_unit, _commit)
//   settleFold   fold one event and run its policies, in memory     (Engine._settle)
//   enqueue      one command a policy dispatched, INSERT OR IGNORE by its derived id
//                — each command its own transaction                (Engine._enqueue)
//   advance      the cursor moves past that event — its own transaction
//
// and a CRASH may fall between any two of them. The store (log, command table,
// cursor) survives; process memory (read model, in-flight pool work, the unit the
// host thread is running) does not. Reopening refolds the read model up to the
// cursor (spec §4), settles what lies past it, and re-runs every attempt still
// `running` under its own id.
//
// What the host thread serialises, and why claims wait for `settled`: after an
// append the engine settles the new events before it does anything else, and a
// reopen settles before its first claim. So a claim always sees cursor == log
// length — zero lag at the moment a procedure reads. The host API (submit, retry)
// runs on that same thread, so it is only ever called between runs: never with a unit
// in hand, pool work in flight, or a settle unfinished (hostIdle).
//
// Placement: `pooled` ⊆ PROCS is drawn at init. Topology rule 3 says only a
// procedure that reads nothing that can change may be pooled: it declares no
// `queries:`, or queries only READ-ONLY models (reference data the host seeds and
// nothing writes; graph.qnt). `rule3` is drawn too, so the suite can show the rule is
// NECESSARY (witnessStalePool) as well as sufficient (freshReads). A pool child never
// sees the host's read model: it has its own copy of every read-only model and nothing
// else (pool_domains). So when rule 3 is broken, a pooled reader of a mutable model
// reads it empty — stale as soon as anything has been folded into it.
//
// Domains: the graph's elements are split between a core domain and a plugin, drawn at
// init (graph.qnt's domainOf). Each domain has its own store: its procedures' commands
// and the events they append, and its own reaction cursor. Command seq is one counter
// across the stores (§5.1), so `table` stays one list in seq order; `log` is the order
// the host thread appended in, across stores, and each store holds its slice of it. The
// engine never records that interleaving durably — a reopen refolds each store up to its
// own cursor (crash, refoldStores) and settles each store past it. That is only the
// global prefix fold if at most one store is ever unsettled, and the unsettled events
// are the last ones appended anywhere: oneStoreUnsettled proves it, and modelsAreFold
// after a crash leans on it.
//
// Fold ORDER, and the harm a reopen can do. A model above is a SET of folded events,
// which hides order. A real projection may care (last write wins, a status machine, a
// list), so `foldSeq` records the order each model was folded in since the engine
// opened: live, that is append order; a reopen refolds store by store, which is append
// order only within a store. A model fed by two stores therefore comes back REORDERED
// after any reopen, and a reader that decides on it (`misreads`) — taking the worst case,
// a projection whose value depends on order — saw a value no run without that reopen
// could have produced: it emits a different event, and nothing repairs the log.
// witnessReorderedRead reaches that. The candidate rule (`rule7`, drawn at init like
// rule 3): a model folded from more than one store must be declared order-independent
// (`orderFree`). readsInLogOrder proves the rule sufficient; the witness, necessary.
//
// Read-only models: the host seeds them before the engine opens, and again into the
// empty read model a reopen hands the engine (spec §4: reference data "must survive").
// readOnlyConstant proves nothing the engine does — append, settle, crash — moves them,
// and witnessPooledReadOnly shows a pooled reader of one running, fresh.
//
// The headline results, against st.qnt's:
//   • effectsOnce holds UNCONDITIONALLY. st needed the application to guard, and
//     only guardable procedures could (witnessDoubleAppend there). Here the append
//     and the ack are one transaction, so a replay can never append a second block.
//   • freshReads holds with pools (given rule 3), although the log's ORDER is no
//     longer the claim order (witnessOutOfOrder), and a pooled procedure may read a
//     read-only model (witnessPooledReadOnly).
//   • freshDispatch and dispatchComplete survive crashes, including a crash
//     between two enqueues of one settle, or between the last and the cursor
//     (witnessResettle): the re-settle re-derives the same ids and the store ignores
//     the ones it holds.
//
// The bounds are constants of `engineSpec`, instantiated at the bottom as `engine` (the
// default main: what is proved) and `engineNarrow` (a one-wide pool). Both feed the
// model-based tests (engine/tests/test_mbt.py), which replay `quint run --mbt` traces on
// the real engine and compare its state with the trace's after every step — so every
// action here ends at a pause of the engine (_mbt.py's _ENDS_AT).
//
//   quint run    engine.qnt --invariant=allInvariants --max-samples=3000 --max-steps=25
//   quint run    engine.qnt --invariant='not(witnessOutOfOrder)'   # expect violation = reachable
//   quint verify engine.qnt --invariant=allInvariants --max-steps=5
//   quint run    engine.qnt --main=engineNarrow --mbt --out-itf='t{seq}.itf.json' --n-traces=10

module engineSpec {
  import graph.* from "./graph"

  // The bounds, instantiated at the bottom of this file: `engine` (what is proved) and
  // `engineNarrow` (a one-wide pool, for the model-based tests).
  const MAX_SUBMISSIONS: int     // host-originated commands
  const MAX_LOG: int             // a policy cycle can be genuinely infinite; bound it
  const MAX_CRASHES: int
  const MAX_FAILS: int           // element raises (or a pool worker dies)
  const POOL_WORKERS: int        // one pool lane, this wide

  type Row = { cmd: Command, status: str }        // status: queued | running | done | failed

  // --- State: the store (durable) -----------------------------------------
  var log: List[Event]           // events table
  var table: List[Row]           // commands table, in enqueue (seq) order
  var cursor: int                // cursors table: events settled through here
  // --- State: the host process (volatile) --------------------------------
  var models: View               // the read model
  var folded: int                // ghost: models == fold of log[0, folded)
  var current: List[int]         // the inline unit the host thread is running (0 or 1 row)
  var inflight: Set[int]         // table rows of pool jobs dispatched, not yet committed
  var pending: List[Command]     // what the event being settled dispatched, not yet enqueued
  var foldSeq: str -> List[int]  // ghost: each model's events, in the order folded since open
  // --- Configuration (fixed after init) ----------------------------------
  var g: Graph
  var pooled: Set[str]           // procedures placed on the pool lane
  var orderFree: Set[str]        // models declared order-independent (their folds commute)
  var rule3: bool                // does the placement obey topology rule 3?
                                 // (g.readOnly, also drawn at init, is the deployment's
                                 // read-only models)
  // --- Ghosts ------------------------------------------------------------
  var submitted: int
  var crashes: int
  var fails: int
  var reran: int                 // claims of a row already `running` (recovery)
  var ignored: int               // policy enqueues the store ignored (re-settle)
  var misreads: int              // decisions taken on a model folded out of append order

  // init draws the configuration; initWith takes it, so a `run` can script a scenario.
  action initWith(gr: Graph, pl: Set[str], free: Set[str]): bool = all {
    g' = gr,
    pooled' = pl,
    orderFree' = free,
    rule3' = pl.forall(p => not(readsMutable(gr, p))),
    log' = [], table' = [], cursor' = 0,
    models' = seedView(gr), folded' = 0, current' = [], inflight' = Set(), pending' = [],
    foldSeq' = MODELS.mapBy(m => []),
    submitted' = 0, crashes' = 0, fails' = 0, reran' = 0, ignored' = 0, misreads' = 0,
  }

  action init = {
    nondet reads = READS.oneOf()
    nondet emits = EMITS.oneOf()
    nondet folds = FOLDS.oneOf()
    nondet reacts = REACTS.oneOf()
    nondet dispatches = DISPATCHES.oneOf()
    nondet interactive = INTERACTIVE.oneOf()
    nondet ro = readOnlyCandidates(folds).oneOf()
    nondet dom = DOMAIN_OF.oneOf()
    nondet pl = PROCS.powerset().oneOf()
    nondet free = MODELS.powerset().oneOf()
    val gr = { reads: reads, emits: emits, folds: folds, reacts: reacts, dispatches: dispatches, interactive: interactive, readOnly: ro, domainOf: dom }
    initWith(gr, pl, free)
  }

  // --- Helpers -------------------------------------------------------------
  val settled = cursor == log.length() and folded == cursor
  val busyIdx = inflight
  val hostIdle = current == [] and inflight == Set() and settled
  val poolRoom = inflight.size() < POOL_WORKERS

  // The store an event lives in, and its place there (its seq - 1).
  def storeOf(i: int): str = domainOfEvent(g, log[i])
  def storeIndex(i: int): int = log.indices().filter(j => j < i and storeOf(j) == storeOf(i)).size()
  // A store's reaction cursor: how many of its events are settled.
  def storeCursor(d: str): int = log.indices().filter(i => i < cursor and storeOf(i) == d).size()
  // What a reopen refolds: every store's events up to that store's own cursor, over the
  // seed — never the global prefix, which no store records.
  def refoldStores: View =
    MODELS.mapBy(m => seedView(g).get(m).union(log.indices().filter(i =>
      g.folds.get(m).contains(log[i].kind) and storeIndex(i) < storeCursor(storeOf(i)))))

  // ...and the ORDER it refolds them in: store by store (DOMAIN_LIST), each in its own
  // order — which is append order only if the model's events come from one store.
  def refoldOrder: str -> List[int] =
    MODELS.mapBy(m => DOMAIN_LIST.foldl([], (acc, d) => range(0, LOG_CAP).foldl(acc, (a, i) =>
      if (i < log.length() and storeOf(i) == d and storeIndex(i) < storeCursor(d) and g.folds.get(m).contains(log[i].kind))
        a.append(i)
      else a)))

  // The stores whose events a model folds: the domains of the procedures that emit them.
  def storesFolding(m: str): Set[str] =
    PROCS.filter(p => g.emits.get(p).intersect(g.folds.get(m)) != Set()).map(p => g.domainOf.get(p))
  // Rule 7 (candidate): a model fed by more than one store is declared order-independent.
  val rule7 = MODELS.forall(m => orderFree.contains(m) or storesFolding(m).size() <= 1)

  pure def ascending(l: List[int]): bool = l.indices().forall(k => k == 0 or l[k - 1] < l[k])
  // Would `elem` decide on a model whose fold order no run without a reopen could have
  // produced? (Order-free models are immune: their folds commute.)
  def misorderedFor(fs: str -> List[int], elem: str): bool =
    g.reads.get(elem).exists(m => not(orderFree.contains(m)) and not(ascending(fs.get(m))))

  // No log outgrows this: a claim needs log.length() < MAX_LOG, and one block appends at
  // most one event per kind. (A constant, so Apalache can unroll a range over it.)
  pure val LOG_CAP = MAX_LOG + MAX_DEGREE + 1

  pure def ids(t: List[Row]): Set[CmdId] = t.indices().map(i => t[i].cmd.id)
  pure def setStatus(t: List[Row], i: int, s: str): List[Row] =
    t.replaceAt(i, { cmd: t[i].cmd, status: s })
  pure def minOf(s: Set[int]): int = s.fold(-1, (a, i) => if (a == -1 or i < a) i else a)

  // _claim_next: an interrupted attempt first (in seq order, its lane must have room),
  // then interactive before batch, then seq — among rows whose lane has room.
  def hasRoom(i: int): bool = not(pooled.contains(table[i].cmd.proc)) or poolRoom
  def claimIndex: int = {
    val recovering = table.indices().filter(i =>
      table[i].status == "running" and not(busyIdx.contains(i)) and hasRoom(i))
    val queued = table.indices().filter(i => table[i].status == "queued" and hasRoom(i))
    val urgent = queued.filter(i => g.interactive.contains(table[i].cmd.proc))
    if (recovering != Set()) minOf(recovering)
    else if (urgent != Set()) minOf(urgent)
    else minOf(queued)
  }

  // Append a block and ack its row: ONE transaction.
  action appendAndAck(i: int, view: View): bool = {
    val emitted = runProcedure(g, table[i].cmd, view, log.length())
    all {
      log' = log.concat(emitted),
      table' = setStatus(table, i, "done"),
    }
  }

  action unchangedConfig = all { g' = g, pooled' = pooled, rule3' = rule3, orderFree' = orderFree }

  // --- Host submits a command ----------------------------------------------
  action submit = {
    nondet p = PROCS.oneOf()
    submitProc(p)
  }
  action submitProc(p: str): bool = {
    all {
      hostIdle,
      submitted < MAX_SUBMISSIONS,
      table' = table.append({ cmd: { id: { origin: "host", at: submitted, target: p }, proc: p, view: emptyView }, status: "queued" }),
      submitted' = submitted + 1,
      log' = log, cursor' = cursor, models' = models, folded' = folded, current' = current, inflight' = inflight,
      crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, pending' = pending, foldSeq' = foldSeq, misreads' = misreads, unchangedConfig,
    }
  }

  // --- Claim (host thread, between units, only when settled) ----------------
  action claim = all {
    current == [],
    settled,
    log.length() < MAX_LOG,
    val i = claimIndex
    all {
      i >= 0,
      table' = setStatus(table, i, "running"),
      reran' = if (table[i].status == "running") reran + 1 else reran,
      if (pooled.contains(table[i].cmd.proc))
        all { inflight' = inflight.union(Set(i)), current' = current }
      else
        all { current' = [i], inflight' = inflight },
    },
    log' = log, cursor' = cursor, models' = models, folded' = folded,
    submitted' = submitted, crashes' = crashes, fails' = fails, ignored' = ignored, pending' = pending, foldSeq' = foldSeq, misreads' = misreads, unchangedConfig,
  }

  // --- The inline unit: run against the live read model, append + ack ------
  action runInline = all {
    current.length() == 1,
    val i = current[0]
    appendAndAck(i, models),
    misreads' = if (misorderedFor(foldSeq, table[current[0]].cmd.proc)) misreads + 1 else misreads,
    foldSeq' = foldSeq,
    current' = [],
    cursor' = cursor, models' = models, folded' = folded, inflight' = inflight,
    submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, pending' = pending, unchangedConfig,
  }

  // --- A pool job finishes; the host thread commits it (only when settled) ---
  action completePool = all {
    current == [],
    settled,
    inflight != Set(),
    nondet j = inflight.oneOf()
    all {
      // The child read its own copy of each read-only model, and nothing else.
      appendAndAck(j, seedView(g)),
      inflight' = inflight.exclude(Set(j)),
    },
    current' = current, cursor' = cursor, models' = models, folded' = folded,
    submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, pending' = pending, foldSeq' = foldSeq, misreads' = misreads, unchangedConfig,
  }

  // --- An element raises (or a pool worker dies): attempt failed, nothing appended
  action failInline = all {
    current.length() == 1,
    fails < MAX_FAILS,
    table' = setStatus(table, current[0], "failed"),
    current' = [], fails' = fails + 1,
    log' = log, cursor' = cursor, models' = models, folded' = folded, inflight' = inflight,
    submitted' = submitted, crashes' = crashes, reran' = reran, ignored' = ignored, pending' = pending, foldSeq' = foldSeq, misreads' = misreads, unchangedConfig,
  }
  action failPool = all {
    current == [],
    settled,
    fails < MAX_FAILS,
    inflight != Set(),
    nondet j = inflight.oneOf()
    all {
      table' = setStatus(table, j, "failed"),
      inflight' = inflight.exclude(Set(j)),
    },
    fails' = fails + 1,
    log' = log, cursor' = cursor, models' = models, folded' = folded, current' = current,
    submitted' = submitted, crashes' = crashes, reran' = reran, ignored' = ignored, pending' = pending, foldSeq' = foldSeq, misreads' = misreads, unchangedConfig,
  }

  // --- The host retries a failed command: a new attempt, never automatic ----
  action retry = {
    nondet i = table.indices().oneOf()
    all {
      hostIdle,
      table.length() > 0,
      table[i].status == "failed",
      table' = setStatus(table, i, "queued"),
      log' = log, cursor' = cursor, models' = models, folded' = folded, current' = current, inflight' = inflight,
      submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, pending' = pending, foldSeq' = foldSeq, misreads' = misreads, unchangedConfig,
    }
  }

  // --- Settle one event: fold, then run its policies (in memory) ------------
  action settleFold = all {
    current == [],
    folded == cursor,
    cursor < log.length(),
    val e = log[cursor]
    val v2 = applyEvent(g, models, e, cursor)
    val fs2 = MODELS.mapBy(m =>
      if (g.folds.get(m).contains(e.kind)) foldSeq.get(m).append(cursor) else foldSeq.get(m))
    val deciders = POLICIES.filter(y => g.reacts.get(y).contains(e.kind) and g.dispatches.get(y) != Set())
    all {
      models' = v2,
      folded' = folded + 1,
      pending' = runPolicies(g, e, cursor, v2),
      foldSeq' = fs2,
      misreads' = if (deciders.exists(y => misorderedFor(fs2, y))) misreads + 1 else misreads,
    },
    log' = log, table' = table, cursor' = cursor, current' = current, inflight' = inflight,
    submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored,
    unchangedConfig,
  }

  // --- ... enqueue what they dispatched, one transaction each (INSERT OR IGNORE)
  action enqueue = all {
    pending != [],
    val c = pending.head()
    if (ids(table).contains(c.id)) all { table' = table, ignored' = ignored + 1 }
    else all { table' = table.append({ cmd: c, status: "queued" }), ignored' = ignored },
    pending' = pending.tail(),
    log' = log, cursor' = cursor, models' = models, folded' = folded, current' = current, inflight' = inflight,
    submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, foldSeq' = foldSeq, misreads' = misreads, unchangedConfig,
  }

  // --- ... and only then the cursor moves past it ---------------------------
  action advance = all {
    folded == cursor + 1,
    pending == [],
    cursor' = folded,
    log' = log, table' = table, models' = models, folded' = folded, current' = current, inflight' = inflight,
    submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, pending' = pending, foldSeq' = foldSeq, misreads' = misreads, unchangedConfig,
  }

  // --- Crash and reopen: memory is gone, the store is not -------------------
  // _recover refolds a fresh read model store by store, each to its own cursor, over the
  // read-only models the host seeds into it; settling past it is the ordinary settle
  // steps, which claims wait for. (They take log[cursor]: the engine settles store by
  // store, which is the same event because only one store is ever unsettled.)
  action crash = all {
    crashes < MAX_CRASHES,
    models' = refoldStores,
    foldSeq' = refoldOrder,
    folded' = cursor,
    current' = [],
    inflight' = Set(),
    pending' = [],
    crashes' = crashes + 1,
    log' = log, table' = table, cursor' = cursor,
    submitted' = submitted, fails' = fails, reran' = reran, ignored' = ignored, misreads' = misreads,
    unchangedConfig,
  }

  val idle = current == [] and inflight == Set() and settled and
    table.indices().forall(i => table[i].status == "done" or table[i].status == "failed")
  action quiescent = all {
    idle or log.length() >= MAX_LOG,
    submitted == MAX_SUBMISSIONS,
    log' = log, table' = table, cursor' = cursor, models' = models, folded' = folded,
    current' = current, inflight' = inflight,
    submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, pending' = pending, foldSeq' = foldSeq, misreads' = misreads, unchangedConfig,
  }

  // --- Exploration bias for the reopen harm (engineReopen; `quint run --init=initCrossStore
  // --step=reopenStep`). Every behaviour these produce is one `init`/`step` can produce;
  // they only skip the ones that cannot reach the harm: graphs with no model fed by two
  // stores and read by someone, and reopens before anything is folded.
  action initCrossStore = {
    nondet reads = READS.oneOf()
    nondet emits = EMITS.oneOf()
    nondet folds = FOLDS.oneOf()
    nondet reacts = REACTS.oneOf()
    nondet dispatches = DISPATCHES.oneOf()
    nondet interactive = INTERACTIVE.oneOf()
    nondet dom = DOMAIN_OF.oneOf()
    nondet pl = PROCS.powerset().oneOf()
    nondet free = MODELS.powerset().oneOf()
    nondet k = KINDS.oneOf()
    nondet reader = ELEMS.oneOf()
    // Built, not filtered: the first two procedures both emit k, from different stores,
    // and k feeds m1, which `reader` reads. Every other relation is drawn as init draws it.
    val a = PROC_LIST[0]
    val b = PROC_LIST[1]
    val m = "m1"
    val gr = {
      reads: reads.set(reader, reads.get(reader).union(Set(m))),
      emits: emits.set(a, emits.get(a).union(Set(k))).set(b, emits.get(b).union(Set(k))),
      folds: folds.set(m, folds.get(m).union(Set(k))),
      reacts: reacts, dispatches: dispatches, interactive: interactive, readOnly: Set(),
      domainOf: dom.set(a, "d1").set(b, "d2"),
    }
    initWith(gr, pl, free)
  }
  action reopenStep = any {
    submit, claim, runInline, completePool, settleFold, enqueue, advance, quiescent,
    all { log.length() >= 2, crash },
  }

  action step = any {
    submit, claim, runInline, completePool, failInline, failPool, retry,
    settleFold, enqueue, advance, crash, quiescent,
  }

  // --- Invariants ----------------------------------------------------------

  // E1. The read model is a fold of the log (the refold-on-open guarantee).
  val modelsAreFold = models == foldPrefix(g, log, folded)

  // E2. The cursor never claims ground the read model has not covered.
  val cursorSound = cursor <= folded and folded <= cursor + 1 and folded <= log.length()

  // E3. Every block is a complete, contiguous, ordered run of its procedure.
  val blocksWellFormed = blocksWellFormedOf(g, log)

  // E4. EXACTLY-ONCE EFFECTS, unconditionally: every cause owns one block. No
  //     application guard, no `guardable` side-condition — the transaction does it.
  val effectsOnce = effectsOnceOf(log)

  // E5. Append and ack are one transaction: a block's command is `done`, and a
  //     `done` command that emits has its block.
  pure def statusOf(t: List[Row], id: CmdId): Set[str] =
    t.indices().filter(i => t[i].cmd.id == id).map(i => t[i].status)
  val ackAtomic = all {
    log.indices().forall(i => statusOf(table, log[i].cause) == Set("done")),
    table.indices().forall(i => (table[i].status == "done" and g.emits.get(table[i].cmd.proc) != Set())
      implies log.indices().exists(k => log[k].cause == table[i].cmd.id)),
    // a failed or unrun attempt appended nothing
    table.indices().forall(i => table[i].status != "done"
      implies not(log.indices().exists(k => log[k].cause == table[i].cmd.id))),
  }

  // E6. Causality of ids.
  val causal = causalOf(g, log)

  // E7. Fresh reads — sequential consistency of every decision — given rule 3.
  val freshReads = rule3 implies freshReadsOf(g, log)

  // E8. Policies read exactly the fold through their trigger, across crashes, in the
  //     log and in the queue.
  val freshDispatch = freshDispatchOf(g, log)
  val freshQueue = {
    val pv = prefixViews(g, log)
    table.indices().forall(i => {
      val c = table[i].cmd
      c.id.origin != "host" implies c.view == restrict(g, c.id.origin, pv.get(c.id.at + 1))
    })
  }

  // E9. Nothing settled is lost: every reacting policy of every settled event has
  //     its command in the table.
  val dispatchComplete =
    log.indices().forall(pos => pos < cursor implies
      POLICIES.forall(y => g.reacts.get(y).contains(log[pos].kind) implies
        g.dispatches.get(y).forall(p => ids(table).contains({ origin: y, at: pos, target: p }))))

  // E10. One row per command id.
  val idsDistinct = ids(table).size() == table.length()

  // E11. Process memory points only at rows that are really running.
  val memorySound = all {
    current.indices().forall(k => table[current[k]].status == "running"),
    inflight.forall(j => table[j].status == "running"),
    current.indices().forall(k => not(busyIdx.contains(current[k]))),
    inflight.size() <= POOL_WORKERS,
    pending != [] implies folded == cursor + 1,
  }

  // E12. Nothing waits behind an empty engine: when idle, every command finished.
  val noLostWork = (idle and log.length() < MAX_LOG) implies
    table.indices().forall(i => table[i].status == "done" or table[i].status == "failed")

  // E13. Read-only models never change: nothing folds one, the read model holds exactly
  //      the seed through every append, settle and crash, and every decision that read
  //      one — inline, pooled, or a policy's — read exactly the seed. Unconditional: it
  //      does not lean on rule 3, so a pool child's own copy is as good as the host's.
  val readOnlyConstant = g.readOnly.forall(m => all {
    g.folds.get(m) == Set(),
    models.get(m) == SEED,
    log.indices().forall(i => all {
      g.reads.get(log[i].cause.target).contains(m) implies log[i].view.get(m) == SEED,
      if (log[i].cause.origin == "host") true
      else g.reads.get(log[i].cause.origin).contains(m) implies log[i].dispatchView.get(m) == SEED,
    }),
  })

  // E14. At most one store is unsettled, and its unsettled events are the last appended
  //      anywhere: the per-store cursors a crash leaves are exactly the global prefix.
  //      (What makes refoldStores, and settling store by store, the global order's.)
  val oneStoreUnsettled =
    log.indices().forall(i => i >= cursor implies storeOf(i) == storeOf(cursor))

  // E15. The fold-order ghost is the read model, ordered: each model's foldSeq holds
  //      exactly its folded events, each once.
  val foldSeqSound = MODELS.forall(m => all {
    foldSeq.get(m).indices().map(k => foldSeq.get(m)[k]).union(seedView(g).get(m)) == models.get(m),
    foldSeq.get(m).indices().map(k => foldSeq.get(m)[k]).size() == foldSeq.get(m).length(),
  })
  // E16. Live, every model folds in append order: the harm needs a reopen.
  val liveFoldsInOrder = crashes == 0 implies MODELS.forall(m => ascending(foldSeq.get(m)))
  // E17. Under rule 7, every model that is not order-free holds its events in append
  //      order, across any number of reopens: a one-store model refolds in its store's
  //      order, and a live settle only ever appends the newest event.
  val foldsInLogOrder = rule7 implies
    MODELS.forall(m => orderFree.contains(m) or ascending(foldSeq.get(m)))
  // E18. ...so under rule 7 no decision is ever taken on a model folded out of order.
  val readsInLogOrder = rule7 implies misreads == 0

  val allInvariants = all {
    modelsAreFold, cursorSound, blocksWellFormed, effectsOnce, ackAtomic, causal,
    freshReads, freshDispatch, freshQueue, dispatchComplete, idsDistinct, memorySound, noLostWork,
    readOnlyConstant, oneStoreUnsettled, foldSeqSound, liveFoldsInOrder, foldsInLogOrder, readsInLogOrder,
  }

  // --- Witnesses (should be REACHABLE: `--invariant=not(w)` must violate) ---
  val witnessCascade    = log.indices().exists(i => log[i].cause.origin != "host")
  val witnessReader     = log.indices().exists(i => log[i].view != emptyView)
  val witnessPooledRun  = log.indices().exists(i => pooled.contains(log[i].cause.target))
  // Two pool jobs genuinely in flight at once.
  val witnessConcurrent = inflight.size() == POOL_WORKERS
  // The log is not in claim order: a block lands after a block whose command was
  // enqueued LATER. Pools make the order a race; the reads stay fresh regardless.
  pure def seqOf(t: List[Row], id: CmdId): int =
    t.indices().filter(i => t[i].cmd.id == id).fold(-1, (a, i) => i)
  val witnessOutOfOrder = rule3 and log.indices().exists(i => log.indices().exists(k =>
    i < k and seqOf(table, log[i].cause) > seqOf(table, log[k].cause)))
  val witnessPriority   = table.length() >= 2 and settled and current == [] and
    claimIndex > 0 and table.indices().forall(i => table[i].status != "running")
  val witnessRecovered  = reran > 0                  // an interrupted attempt re-ran under its id
  val witnessCrashAfterAppend = crashes > 0 and log.length() > 0
  val witnessResettle   = ignored > 0                // a re-settle re-derived ids the store held
  val witnessFailed     = table.indices().exists(i => table[i].status == "failed")
  // Every failure was retried by the host and the run still finished clean.
  val witnessRetried    = fails > 0 and idle and table.indices().forall(i => table[i].status == "done")
  val witnessBound      = log.length() >= MAX_LOG
  // Both stores hold events, and a policy of one domain dispatched a command another
  // domain's procedure ran.
  val witnessTwoStores = log.indices().exists(i => log.indices().exists(k => storeOf(i) != storeOf(k)))
  val witnessCrossDomain = log.indices().exists(i =>
    log[i].cause.origin != "host" and g.domainOf.get(log[i].cause.origin) != storeOf(i))
  // THE HARM: a reopen refolded a model fed by two stores out of append order, and a
  // procedure or policy decided on it. (Necessarily not rule 7.)
  val witnessReorderedRead = misreads > 0
  // Rule 7's declaration at work: an order-free model really was refolded out of order,
  // and that is fine, because its folds commute.
  val witnessOrderFreeReordered = rule7 and orderFree.exists(m => not(ascending(foldSeq.get(m))))
  // Rule 3 admits a reader of read-only models to a pool, and it ran there.
  val witnessPooledReadOnly = rule3 and log.indices().exists(i =>
    pooled.contains(log[i].cause.target) and g.reads.get(log[i].cause.target) != Set())
  // Rule 3 is NECESSARY: pool a reader of a mutable model and it decides on a stale one.
  val witnessStalePool  = not(rule3) and not(freshReadsOf(g, log))
}

// What the suite proves and simulates: `quint run engine.qnt` selects it by default.
module engine {
  import graph.* from "./graph"
  import engineSpec(MAX_SUBMISSIONS = 2, MAX_LOG = 6, MAX_CRASHES = 2, MAX_FAILS = 1, POOL_WORKERS = 2).*

  // --- The reopen harm, as a scenario (`quint test engine.qnt --main=engine`) -------
  // p2 (the plugin, d2) emits e1; p1 (the core, d1) emits e1 then e2; model m1 folds e1;
  // policy y1 reacts to e2, reads m1 and dispatches. p2 runs first, so m1 folds position
  // 0 (plugin store) then 1 (core store). The engine settles p1's e1, then crashes before
  // settling its e2. The reopen refolds the core store first — m1 = [1, 0] — and then
  // settles e2: y1 decides on m1 folded out of append order. With an order-sensitive
  // projection, that is a value no run without the crash (or any restart) would give y1.
  pure val HARM_GRAPH: Graph = {
    reads: Map("p1" -> Set(), "p2" -> Set(), "y1" -> Set("m1")),
    emits: Map("p1" -> Set("e1", "e2"), "p2" -> Set("e1")),
    folds: Map("m1" -> Set("e1"), "m2" -> Set()),
    reacts: Map("y1" -> Set("e2")),
    dispatches: Map("y1" -> Set("p2")),
    interactive: Set(),
    readOnly: Set(),
    domainOf: Map("p1" -> "d1", "p2" -> "d2", "y1" -> "d1"),
  }

  action harmScenario(gr: Graph, free: Set[str]): bool =
    initWith(gr, Set(), free)
      .then(submitProc("p2")).then(submitProc("p1"))
      .then(claim).then(runInline).then(settleFold).then(advance)   // p2: m1 = [0]
      .then(claim).then(runInline).then(settleFold).then(advance)   // p1's e1: m1 = [0, 1]
      .then(crash)                                                  // reopen: m1 = [1, 0]
      .then(settleFold)                                             // p1's e2: y1 reads m1

  // The harm: y1 decided on a reordered m1. Rule 7 does not hold.
  run reopenReordersTest =
    harmScenario(HARM_GRAPH, Set())
      .expect(all { foldSeq.get("m1") == [1, 0], witnessReorderedRead, not(rule7), allInvariants })

  // Declared order-independent: the same reorder happens, and harms nothing.
  run orderFreeAbsorbsTest =
    harmScenario(HARM_GRAPH, Set("m1"))
      .expect(all { foldSeq.get("m1") == [1, 0], misreads == 0, witnessOrderFreeReordered, allInvariants })

  // Both procedures in one store: the reopen refolds in append order.
  run oneStoreKeepsOrderTest =
    harmScenario({ ...HARM_GRAPH, domainOf: Map("p1" -> "d1", "p2" -> "d1", "y1" -> "d1") }, Set())
      .expect(all { foldSeq.get("m1") == [0, 1], misreads == 0, rule7, allInvariants })
}

// Reopens, and nothing else rare: no element failures, one crash, a one-wide pool. The
// reopen harm (witnessReorderedRead) takes ~13 steps of exactly the right kind, beyond
// Apalache's depth and rare under sampling; here the sampler spends its steps on runs,
// settles and the one reopen, so rule 7's invariants are sampled where they can fail.
module engineReopen {
  import engineSpec(MAX_SUBMISSIONS = 2, MAX_LOG = 6, MAX_CRASHES = 1, MAX_FAILS = 0, POOL_WORKERS = 1).*
}

// A one-wide pool and a third submission: the model-based tests (engine/tests/test_mbt.py)
// draw traces from here too, because at width 2 a full pool with more pool work queued
// behind it is rare, and that is where a lane's capacity is tested.
module engineNarrow {
  import engineSpec(MAX_SUBMISSIONS = 3, MAX_LOG = 6, MAX_CRASHES = 2, MAX_FAILS = 1, POOL_WORKERS = 1).*
}
