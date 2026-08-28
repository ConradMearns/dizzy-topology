// specs/st.qnt — Spec 1: the single-threaded engine (dizzy.engine.st), over an
// ARBITRARY feature graph.
//
// One hybrid consumer group: one inline lane over the durable command queue,
// one cursor over the log, both served by one thread. Engine.run_command —
// claim → run_procedure → ingest(append → fold → commit → run policies →
// dispatch) — is ONE atomic step (engine/loop.py:254-402). That atomicity is
// what "sequentially consistent" means for Dizzy; lag is zero by construction
// (`cursor == log.length()` is an invariant here and a *choice* in groups.qnt).
//
// Nondeterminism, deliberately:
//   • the feature graph (init)                — every feature of the bounded size
//   • `guarded` (init)                        — whether the application bothers to
//                                                dedupe redeliveries. The ENGINE does
//                                                not (st/executor.py:49-56: a replay
//                                                "appends the fact again ... nothing
//                                                dedups it"); topology.md:347-351 asks
//                                                it of the application. The guard is
//                                                modelled as the only one a procedure
//                                                CAN implement — "my own block is
//                                                already in a model I read" — so
//                                                exactly-once holds only for
//                                                `guardable` procedures (graph.qnt).
//   • which command the host submits, and when
//   • redelivery: a crash between ingest and ack re-queues the claimed row at
//     the FRONT of its priority level (command_queue.py:409-414, claim order
//     `priority DESC, id`)
//
// Out of scope here, by design (see groups.qnt / Spec 3 seeds): `executor: pool`
// lanes, an element raising mid-emission (a partial block), replication
// (`add_replicated`, which breaks prefix positions), host `put` interleaving
// between two dispatches of one cascade.
//
//   quint run    st.qnt --invariant=allInvariants --max-samples=3000 --max-steps=20
//   quint run    st.qnt --invariant='not(witnessDoubleAppend)'   # expect violation = reachable
//   quint verify st.qnt --invariant=allInvariants --max-steps=3  # Apalache; deeper is infeasible today

module st {
  import graph.* from "./graph"

  pure val MAX_SUBMISSIONS = 2   // host-originated commands
  pure val MAX_LOG = 6           // a policy cycle can be genuinely infinite; bound it

  // --- State -------------------------------------------------------------
  var g: Graph               // the feature (fixed after init)
  var guarded: bool          // application-level dedupe assumption (fixed after init)
  var queue: List[Command]   // st.DurableCommandQueue — one lane
  var log: List[Event]       // EventStore, the only shared truth
  var cursor: int            // folded prefix length
  var models: View           // read models (positions folded per model)
  var ran: Set[Command]      // commands whose procedure has run (a redelivery replays the row verbatim)
  var submitted: int
  var noops: int             // ghost: redeliveries the application guard absorbed

  action init = {
    nondet reads = READS.oneOf()
    nondet emits = EMITS.oneOf()
    nondet folds = FOLDS.oneOf()
    nondet reacts = REACTS.oneOf()
    nondet dispatches = DISPATCHES.oneOf()
    nondet interactive = INTERACTIVE.oneOf()
    nondet gd = Set(true, false).oneOf()
    all {
      g' = { reads: reads, emits: emits, folds: folds, reacts: reacts, dispatches: dispatches, interactive: interactive },
      guarded' = gd,
      queue' = [], log' = [], cursor' = 0, models' = emptyView, ran' = Set(), submitted' = 0, noops' = 0,
    }
  }

  // --- Host submits a command ------------------------------------------------
  action submit = {
    nondet p = PROCS.oneOf()
    all {
      submitted < MAX_SUBMISSIONS,
      queue' = queue.append({ id: { origin: "host", at: submitted, target: p }, proc: p, view: emptyView }),
      submitted' = submitted + 1,
      g' = g, guarded' = guarded, log' = log, cursor' = cursor, models' = models, ran' = ran, noops' = noops,
    }
  }

  // --- Claim: LaneExecutor's two-level priority (command_queue.py:248-251) ---
  // The earliest interactive command wins; otherwise the earliest command.
  pure def claimIndex(gr: Graph, q: List[Command]): int = {
    val urgent = q.indices().filter(i => gr.interactive.contains(q[i].proc))
    if (urgent != Set()) urgent.fold(-1, (a, i) => if (a == -1 or i < a) i else a)
    else 0
  }
  pure def without(q: List[Command], i: int): List[Command] =
    q.slice(0, i).concat(q.slice(i + 1, q.length()))

  // --- ingest: append, fold, commit, run policies — per event, in order ------
  // Mirrors Engine.apply_event exactly; the policies' view is the models
  // as folded *through this event* (fold → commit → policies).
  pure def ingest(gr: Graph, l: List[Event], v: View, events: List[Event]):
      { log: List[Event], models: View, dispatched: List[Command] } =
    events.foldl({ log: l, models: v, dispatched: [] }, (acc, e) => {
      val pos = acc.log.length()
      val v2 = applyEvent(gr, acc.models, e, pos)
      {
        log: acc.log.append(e),
        models: v2,
        dispatched: acc.dispatched.concat(runPolicies(gr, e, pos, v2)),
      }
    })

  // --- The engine step -----------------------------------------------------
  action runCommand = all {
    queue.length() > 0,
    log.length() < MAX_LOG,
    val i = claimIndex(g, queue)
    val cmd = queue[i]
    val rest = without(queue, i)
    if (guarded and seesOwnBlock(g, log, models, cmd))
      // application-level guard: the procedure sees its own earlier block in a model it reads
      all { queue' = rest, log' = log, cursor' = cursor, models' = models, ran' = ran, noops' = noops + 1 }
    else {
      val emitted = runProcedure(g, cmd, models, log.length())
      val r = ingest(g, log, models, emitted)
      all {
        log' = r.log,
        models' = r.models,
        cursor' = r.log.length(),
        queue' = rest.concat(r.dispatched),  // dispatch is to the external queue (loop.py:152-158)
        ran' = ran.union(Set(cmd)),
        noops' = noops,
      }
    },
    g' = g, guarded' = guarded, submitted' = submitted,
  }

  // --- At-least-once: a crash between ingest and ack recovers the row --------
  // The row keeps its id, so it re-enters at the front of its priority level.
  action redeliver = {
    nondet c = ran.oneOf()
    all {
      ran != Set(),
      not(queue.indices().exists(i => queue[i].id == c.id)),   // one DB row: cannot be queued twice
      queue' = [c].concat(queue),
      g' = g, guarded' = guarded, log' = log, cursor' = cursor, models' = models, ran' = ran, submitted' = submitted, noops' = noops,
    }
  }

  // End of run: explicit so the checker does not call it a deadlock.
  action quiescent = all {
    queue.length() == 0 or log.length() >= MAX_LOG,
    submitted == MAX_SUBMISSIONS,
    g' = g, guarded' = guarded, queue' = queue, log' = log, cursor' = cursor, models' = models, ran' = ran, submitted' = submitted, noops' = noops,
  }

  action step = any { submit, runCommand, redeliver, quiescent }

  // --- Invariants ----------------------------------------------------------

  // I1. No lag.
  val zeroLag = cursor == log.length()

  // I2. Convergence: carried models are a refold of the log (this IS the rebuild guarantee).
  val modelsAreFold = models == foldPrefix(g, log, log.length())

  // I3. Every block is a complete run of its procedure, in emit order.
  val blocksWellFormed = blocksWellFormedOf(g, log)

  // I3'. Exactly-once effects — for GUARDABLE procedures, when the application
  //      guards. A procedure that reads no model folding its own emits cannot
  //      recognise its replay; nothing in the engine helps it (executor.py:49-56).
  val effectsOnce = guarded implies effectsOnceGuardableOf(g, log)

  // I4. Causality of ids.
  val causal = causalOf(g, log)

  // I5. Fresh reads — THE sequential-consistency property, per block.
  val freshReads = freshReadsOf(g, log)

  // I6. Policies read fresh too, visible at log level via dispatchView …
  val freshDispatch = freshDispatchOf(g, log)
  // … and in the queue, for commands not yet run.
  val freshQueue = {
    val pv = prefixViews(g, log)
    queue.indices().forall(i => {
      val c = queue[i]
      c.id.origin != "host" implies
        c.view == restrict(g, c.id.origin, pv.get(c.id.at + 1))
    })
  }

  // I7. Completeness — an engine that drops work must FAIL these:
  //   every reacting policy of every folded event has dispatched its command …
  val queuedIds = queue.indices().map(i => queue[i].id)
  val ranIds    = ran.map(c => c.id)
  val dispatchComplete =
    log.indices().forall(pos =>
      POLICIES.forall(y => g.reacts.get(y).contains(log[pos].kind) implies
        g.dispatches.get(y).forall(p => {
          val id = { origin: y, at: pos, target: p }
          queuedIds.contains(id) or ranIds.contains(id)
        })))
  //   … and every command that ran left its block in the log.
  val emitComplete =
    ran.forall(c => g.emits.get(c.proc) == Set() or log.indices().exists(i => log[i].cause == c.id))

  // I8. One DB row per command: no two queued commands share an id.
  val queueIdsDistinct = queuedIds.size() == queue.length()

  val allInvariants = all {
    zeroLag, modelsAreFold, blocksWellFormed, effectsOnce, causal,
    freshReads, freshDispatch, freshQueue, dispatchComplete, emitComplete, queueIdsDistinct
  }

  // --- Witnesses (should be REACHABLE: `--invariant=not(w)` must violate) ---
  val witnessCascade      = log.indices().exists(i => log[i].cause.origin != "host")
  val witnessReader       = log.indices().exists(i => log[i].view != emptyView)
  val witnessPolicyReader = log.indices().exists(i => log[i].dispatchView != emptyView)
  val witnessPriority     = queue.length() >= 2 and claimIndex(g, queue) != 0
  val witnessBound        = log.length() >= MAX_LOG
  val witnessRedelivered  = queue.indices().exists(i => ran.exists(c => c.id == queue[i].id))
  val witnessGuardNoop    = noops > 0
  val witnessDoubleAppend = not(effectsOnceOf(log))
  // Even a guarding application double-appends when the procedure is not guardable.
  val witnessUnguardable  = guarded and not(effectsOnceOf(log))
}
