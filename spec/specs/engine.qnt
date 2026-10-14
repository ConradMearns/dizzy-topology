// specs/engine.qnt — Spec 4: the topology engine (engine/src/dizzy_topology_engine),
// over an ARBITRARY feature graph and an arbitrary rule-3 placement.
//
// The topology engine is the Simple Engine (ext/dizzy/docs/design/simple-engine.md)
// with lanes: an inline lane served on the host thread, and a pool lane whose
// procedures run in worker processes while the host thread keeps claiming. Unlike
// st.qnt (the pre-Simple-Engine `dizzy.engine.st`), the store's transaction
// boundaries are what make it correct, so they are modelled one step each:
//
//   claim        attempt `running`, durably, before anything runs  (_claim_next)
//   append+ack   the block AND the command's `done`, ONE transaction (_unit, _commit)
//   settleFold   fold one event, then its policies enqueue by derived id with
//                INSERT OR IGNORE                                  (Engine._settle)
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
// length — zero lag at the moment a procedure reads.
//
// Placement: `pooled` ⊆ PROCS is drawn at init. Topology rule 3 says only a
// procedure that declares no `queries:` may be pooled; `rule3` is drawn too, so the
// suite can show the rule is NECESSARY (witnessStalePool) as well as sufficient
// (freshReads). A pool child never sees the host's read model; when rule 3 is broken
// it is modelled as reading the snapshot taken when its job was dispatched — the most
// charitable thing an implementation could do — and that is still stale.
//
// The headline results, against st.qnt's:
//   • effectsOnce holds UNCONDITIONALLY. st needed the application to guard, and
//     only guardable procedures could (witnessDoubleAppend there). Here the append
//     and the ack are one transaction, so a replay can never append a second block.
//   • freshReads holds with pools (given rule 3), although the log's ORDER is no
//     longer the claim order (witnessOutOfOrder).
//   • freshDispatch and dispatchComplete survive crashes, including a crash
//     between a policy's enqueue and the cursor (witnessResettle): the re-settle
//     re-derives the same ids and the store ignores them.
//
//   quint run    engine.qnt --invariant=allInvariants --max-samples=3000 --max-steps=25
//   quint run    engine.qnt --invariant='not(witnessOutOfOrder)'   # expect violation = reachable
//   quint verify engine.qnt --invariant=allInvariants --max-steps=4

module engine {
  import graph.* from "./graph"

  pure val MAX_SUBMISSIONS = 2   // host-originated commands
  pure val MAX_LOG = 6           // a policy cycle can be genuinely infinite; bound it
  pure val MAX_CRASHES = 2
  pure val MAX_FAILS = 1         // element raises (or a pool worker dies)
  pure val POOL_WORKERS = 2      // one pool lane, this wide

  type Row = { cmd: Command, status: str }        // status: queued | running | done | failed
  type Job = { idx: int, view: View }             // a pool job: table row + what it could read

  // --- State: the store (durable) -----------------------------------------
  var log: List[Event]           // events table
  var table: List[Row]           // commands table, in enqueue (seq) order
  var cursor: int                // cursors table: events settled through here
  // --- State: the host process (volatile) --------------------------------
  var models: View               // the read model
  var folded: int                // ghost: models == fold of log[0, folded)
  var current: List[int]         // the inline unit the host thread is running (0 or 1 row)
  var inflight: Set[Job]         // pool jobs dispatched and not yet committed
  // --- Configuration (fixed after init) ----------------------------------
  var g: Graph
  var pooled: Set[str]           // procedures placed on the pool lane
  var rule3: bool                // does the placement obey topology rule 3?
  // --- Ghosts ------------------------------------------------------------
  var submitted: int
  var crashes: int
  var fails: int
  var reran: int                 // claims of a row already `running` (recovery)
  var ignored: int               // policy enqueues the store ignored (re-settle)

  pure def isReaderProc(gr: Graph, p: str): bool = gr.reads.get(p) != Set()

  action init = {
    nondet reads = READS.oneOf()
    nondet emits = EMITS.oneOf()
    nondet folds = FOLDS.oneOf()
    nondet reacts = REACTS.oneOf()
    nondet dispatches = DISPATCHES.oneOf()
    nondet interactive = INTERACTIVE.oneOf()
    nondet pl = PROCS.powerset().oneOf()
    val gr = { reads: reads, emits: emits, folds: folds, reacts: reacts, dispatches: dispatches, interactive: interactive }
    all {
      g' = gr,
      pooled' = pl,
      rule3' = pl.forall(p => not(isReaderProc(gr, p))),
      log' = [], table' = [], cursor' = 0,
      models' = emptyView, folded' = 0, current' = [], inflight' = Set(),
      submitted' = 0, crashes' = 0, fails' = 0, reran' = 0, ignored' = 0,
    }
  }

  // --- Helpers -------------------------------------------------------------
  val settled = cursor == log.length() and folded == cursor
  val busyIdx = inflight.map(j => j.idx)
  val poolRoom = inflight.size() < POOL_WORKERS

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

  action unchangedConfig = all { g' = g, pooled' = pooled, rule3' = rule3 }

  // --- Host submits a command ----------------------------------------------
  action submit = {
    nondet p = PROCS.oneOf()
    all {
      submitted < MAX_SUBMISSIONS,
      table' = table.append({ cmd: { id: { origin: "host", at: submitted, target: p }, proc: p, view: emptyView }, status: "queued" }),
      submitted' = submitted + 1,
      log' = log, cursor' = cursor, models' = models, folded' = folded, current' = current, inflight' = inflight,
      crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, unchangedConfig,
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
        // A pool child gets no read model. Under rule 3 the procedure reads nothing, so
        // the snapshot is irrelevant; without it, this is the freshest view it could have.
        all { inflight' = inflight.union(Set({ idx: i, view: models })), current' = current }
      else
        all { current' = [i], inflight' = inflight },
    },
    log' = log, cursor' = cursor, models' = models, folded' = folded,
    submitted' = submitted, crashes' = crashes, fails' = fails, ignored' = ignored, unchangedConfig,
  }

  // --- The inline unit: run against the live read model, append + ack ------
  action runInline = all {
    current.length() == 1,
    val i = current[0]
    appendAndAck(i, models),
    current' = [],
    cursor' = cursor, models' = models, folded' = folded, inflight' = inflight,
    submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, unchangedConfig,
  }

  // --- A pool job finishes; the host thread commits it (only when settled) ---
  action completePool = all {
    current == [],
    settled,
    inflight != Set(),
    nondet j = inflight.oneOf()
    all {
      appendAndAck(j.idx, j.view),
      inflight' = inflight.exclude(Set(j)),
    },
    current' = current, cursor' = cursor, models' = models, folded' = folded,
    submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, unchangedConfig,
  }

  // --- An element raises (or a pool worker dies): attempt failed, nothing appended
  action failInline = all {
    current.length() == 1,
    fails < MAX_FAILS,
    table' = setStatus(table, current[0], "failed"),
    current' = [], fails' = fails + 1,
    log' = log, cursor' = cursor, models' = models, folded' = folded, inflight' = inflight,
    submitted' = submitted, crashes' = crashes, reran' = reran, ignored' = ignored, unchangedConfig,
  }
  action failPool = all {
    current == [],
    settled,
    fails < MAX_FAILS,
    inflight != Set(),
    nondet j = inflight.oneOf()
    all {
      table' = setStatus(table, j.idx, "failed"),
      inflight' = inflight.exclude(Set(j)),
    },
    fails' = fails + 1,
    log' = log, cursor' = cursor, models' = models, folded' = folded, current' = current,
    submitted' = submitted, crashes' = crashes, reran' = reran, ignored' = ignored, unchangedConfig,
  }

  // --- The host retries a failed command: a new attempt, never automatic ----
  action retry = {
    nondet i = table.indices().oneOf()
    all {
      table.length() > 0,
      table[i].status == "failed",
      table' = setStatus(table, i, "queued"),
      log' = log, cursor' = cursor, models' = models, folded' = folded, current' = current, inflight' = inflight,
      submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, unchangedConfig,
    }
  }

  // --- Settle one event: fold, then its policies enqueue (INSERT OR IGNORE) --
  action settleFold = all {
    current == [],
    folded == cursor,
    cursor < log.length(),
    val e = log[cursor]
    val v2 = applyEvent(g, models, e, cursor)
    val dispatched = runPolicies(g, e, cursor, v2)
    val r = dispatched.foldl({ t: table, dup: 0 }, (acc, c) =>
      if (ids(acc.t).contains(c.id)) { t: acc.t, dup: acc.dup + 1 }
      else { t: acc.t.append({ cmd: c, status: "queued" }), dup: acc.dup })
    all {
      models' = v2,
      folded' = folded + 1,
      table' = r.t,
      ignored' = ignored + r.dup,
    },
    log' = log, cursor' = cursor, current' = current, inflight' = inflight,
    submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, unchangedConfig,
  }

  // --- ... and only then the cursor moves past it ---------------------------
  action advance = all {
    folded == cursor + 1,
    cursor' = folded,
    log' = log, table' = table, models' = models, folded' = folded, current' = current, inflight' = inflight,
    submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, unchangedConfig,
  }

  // --- Crash and reopen: memory is gone, the store is not -------------------
  // _recover refolds a fresh read model to the cursor; settling past it is the
  // ordinary settle steps, which claims wait for.
  action crash = all {
    crashes < MAX_CRASHES,
    models' = foldPrefix(g, log, cursor),
    folded' = cursor,
    current' = [],
    inflight' = Set(),
    crashes' = crashes + 1,
    log' = log, table' = table, cursor' = cursor,
    submitted' = submitted, fails' = fails, reran' = reran, ignored' = ignored, unchangedConfig,
  }

  val idle = current == [] and inflight == Set() and settled and
    table.indices().forall(i => table[i].status == "done" or table[i].status == "failed")
  action quiescent = all {
    idle or log.length() >= MAX_LOG,
    submitted == MAX_SUBMISSIONS,
    log' = log, table' = table, cursor' = cursor, models' = models, folded' = folded,
    current' = current, inflight' = inflight,
    submitted' = submitted, crashes' = crashes, fails' = fails, reran' = reran, ignored' = ignored, unchangedConfig,
  }

  action step = any {
    submit, claim, runInline, completePool, failInline, failPool, retry,
    settleFold, advance, crash, quiescent,
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
    inflight.forall(j => table[j.idx].status == "running"),
    current.indices().forall(k => not(busyIdx.contains(current[k]))),
    inflight.size() <= POOL_WORKERS,
  }

  // E12. Nothing waits behind an empty engine: when idle, every command finished.
  val noLostWork = (idle and log.length() < MAX_LOG) implies
    table.indices().forall(i => table[i].status == "done" or table[i].status == "failed")

  val allInvariants = all {
    modelsAreFold, cursorSound, blocksWellFormed, effectsOnce, ackAtomic, causal,
    freshReads, freshDispatch, freshQueue, dispatchComplete, idsDistinct, memorySound, noLostWork,
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
  // Rule 3 is NECESSARY: pool a reader and it decides on a stale model.
  val witnessStalePool  = not(rule3) and not(freshReadsOf(g, log))
}
