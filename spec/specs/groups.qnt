// specs/groups.qnt — Spec 2: consumer groups, i.e. the distributed engine.
//
// The same feature graph, split across GROUPS — each a process (or device)
// with ONE cursor over the shared log and ONE lane of the durable command
// queue (dizzy/engine/groups.py, topology.md "What a group worker does").
//
// Placement is a map `owner : PROCS ∪ POLICIES ∪ MODELS → GROUPS`, chosen
// nondeterministically at init alongside the graph, so a bounded check ranges
// over every (feature, topology) pair of the bounded size — including the
// one-group placement, which must behave exactly like st (`singleGroupIsSt`).
// Making the MODEL the unit of placement bakes in rule 1. Rule 3 (worker
// counts) is out of scope: every group is one lane.
//
// Shared vs per-group:
//   • log          shared, append-only, totally ordered (the EventStore)
//   • substrate    shared read models (one sqla DB); model m is folded ONLY by
//                  owner(m), so it reflects owner(m)'s progress, not the tail
//   • cursor[h]    per group, durable; `foldOne` advances it by one entry,
//                  running h's projections then h's policies on that entry
//   • lane[h]      per group; commands whose procedure h owns
//   • pending[h]   the command h has claimed and not yet run
//   • transport    a doorbell only, so not modelled: a fold may happen at any
//                  time ("a dropped notification costs latency, never work")
//
// The worker's cycle (groups.py:419-426) is
//     fold_to_tail(); claim(); fold_to_tail(); execute(); fold_to_tail()
// with NO lock between the middle fold returning and the procedure's first
// read (groups.py:44-50). So it is two steps here: `claim` (guarded by
// cursor-at-tail: the middle fold has returned) and `execute` (unguarded: other
// groups may have appended in between). That window is real, and it is why
// the theorem below is NOT "a rule-6-legal split reads what st reads":
//
//   what groups.py promises (:27-29): the read model has committed THROUGH
//   THE COMMAND'S CAUSE before the procedure runs         → rule6 ⇒ causeFolded
//   what st gives: the read model is folded to the tail   → freshReads
//
// `witnessClaimWindow` shows rule6 ∧ ¬freshReads is reachable: a rule-6-legal
// deployment can still decide on a model that is behind the log, i.e. the
// "byte-identical models" claim in topology.md:322-326 does not hold for the
// worker as written. Closing the window (a lock, or re-checking the tail after
// the claim) would make `freshReads` provable again — that is a design choice
// for Dizzy, surfaced here rather than assumed away.
//
// Faults modelled: at-least-once command redelivery (`redeliver`) and cursor
// loss (`rewind`: commit-then-save, cursors.py:33-46 — models keep their
// committed state, the cursor goes back, entries are re-folded and policies
// re-dispatch under the same deterministic ids).
//
// Deferred to Spec 3 (seeds): per-device substrate replicas, sharded
// event-only policy groups (k/N), a group folding its own appends ahead of
// earlier foreign entries, lane-file loss.
//
//   quint run    groups.qnt --invariant=allInvariants --max-samples=3000 --max-steps=20
//   quint run    groups.qnt --invariant='not(witnessClaimWindow)'  # expect violation = reachable
//   quint verify groups.qnt --invariant=allInvariants --max-steps=3

module groups {
  import graph.* from "./graph"

  pure val PLACEABLE = ELEMS.union(MODELS)
  pure val PLACEMENTS = PLACEABLE.setOfMaps(GROUPS)

  pure val MAX_SUBMISSIONS = 2
  pure val MAX_LOG = 6

  // --- State -------------------------------------------------------------
  var g: Graph
  var guarded: bool
  var owner: str -> str        // the topology
  var log: List[Event]
  var substrate: View          // shared read models, each folded by its owner
  var cursor: str -> int       // per group, durable
  var hwm: str -> int          // high-water mark of cursor (what is really folded) — a ghost
  var lane: str -> List[Command]
  var pending: str -> List[Command]   // at most one: the claimed, not-yet-run command
  var ran: Set[Command]
  var submitted: int
  var rewound: bool            // ghost: a cursor was lost at some point

  action init = {
    nondet reads = READS.oneOf()
    nondet emits = EMITS.oneOf()
    nondet folds = FOLDS.oneOf()
    nondet reacts = REACTS.oneOf()
    nondet dispatches = DISPATCHES.oneOf()
    nondet interactive = INTERACTIVE.oneOf()
    nondet gd = Set(true, false).oneOf()
    nondet placement = PLACEMENTS.oneOf()
    all {
      g' = { reads: reads, emits: emits, folds: folds, reacts: reacts, dispatches: dispatches, interactive: interactive },
      guarded' = gd,
      owner' = placement,
      log' = [], substrate' = emptyView,
      cursor' = GROUPS.mapBy(h => 0), hwm' = GROUPS.mapBy(h => 0),
      lane' = GROUPS.mapBy(h => []), pending' = GROUPS.mapBy(h => []),
      ran' = Set(), submitted' = 0, rewound' = false,
    }
  }

  // --- Helpers ---------------------------------------------------------------
  pure def claimIndex(gr: Graph, q: List[Command]): int = {
    val urgent = q.indices().filter(i => gr.interactive.contains(q[i].proc))
    if (urgent != Set()) urgent.fold(-1, (a, i) => if (a == -1 or i < a) i else a)
    else 0
  }
  pure def without(q: List[Command], i: int): List[Command] =
    q.slice(0, i).concat(q.slice(i + 1, q.length()))
  pure def route(o: str -> str, lanes: str -> List[Command], cmds: List[Command]): str -> List[Command] =
    cmds.foldl(lanes, (ls, c) => ls.set(o.get(c.proc), ls.get(o.get(c.proc)).append(c)))

  // A group with nothing that consumes the log holds no cursor (topology.md
  // "a procedure group has no cursor"), so it is not gated on folding.
  def consumesLog(h: str): bool =
    MODELS.exists(m => owner.get(m) == h) or POLICIES.exists(y => owner.get(y) == h)

  action fixedConfig = all {
    g' = g, guarded' = guarded, owner' = owner, submitted' = submitted, rewound' = rewound,
  }

  // --- Host submits a command ------------------------------------------------
  action submit = {
    nondet p = PROCS.oneOf()
    val c = { id: { origin: "host", at: submitted, target: p }, proc: p, view: emptyView }
    all {
      submitted < MAX_SUBMISSIONS,
      lane' = lane.set(owner.get(p), lane.get(owner.get(p)).append(c)),
      submitted' = submitted + 1,
      g' = g, guarded' = guarded, owner' = owner, log' = log, substrate' = substrate,
      cursor' = cursor, hwm' = hwm, pending' = pending, ran' = ran, rewound' = rewound,
    }
  }

  // --- Event intake: group h folds ONE entry (Engine.apply_event, h's subset)
  // fold h's projections → commit → run h's policies on the entry → dispatch → save cursor.
  action foldOne = {
    nondet h = GROUPS.oneOf()
    val pos = cursor.get(h)
    all {
      pos < log.length(),
      val e = log[pos]
      val sub2 = MODELS.mapBy(m =>
        if (owner.get(m) == h and g.folds.get(m).contains(e.kind)) substrate.get(m).union(Set(pos))
        else substrate.get(m))
      val dispatched = runPolicies(g, e, pos, sub2).select(c => owner.get(c.id.origin) == h)
      all {
        substrate' = sub2,
        lane' = route(owner, lane, dispatched),
        cursor' = cursor.set(h, pos + 1),
        hwm' = hwm.set(h, if (pos + 1 > hwm.get(h)) pos + 1 else hwm.get(h)),
      },
      fixedConfig, log' = log, pending' = pending, ran' = ran,
    }
  }

  // --- Command intake, step 1: claim (the middle fold has returned) ----------
  action claim = {
    nondet h = GROUPS.oneOf()
    val q = lane.get(h)
    all {
      q.length() > 0,
      pending.get(h).length() == 0,
      not(consumesLog(h)) or cursor.get(h) == log.length(),
      val i = claimIndex(g, q)
      all {
        pending' = pending.set(h, [q[i]]),
        lane' = lane.set(h, without(q, i)),
      },
      fixedConfig, log' = log, substrate' = substrate, cursor' = cursor, hwm' = hwm, ran' = ran,
    }
  }

  // --- Command intake, step 2: run the claimed command (no cursor guard) -------
  action execute = {
    nondet h = GROUPS.oneOf()
    all {
      pending.get(h).length() == 1,
      log.length() < MAX_LOG,
      val cmd = pending.get(h)[0]
      if (guarded and seesOwnBlock(g, log, substrate, cmd))
        all { log' = log, ran' = ran }
      else all {
        log' = log.concat(runProcedure(g, cmd, substrate, log.length())),   // reads the SHARED substrate, now
        ran' = ran.union(Set(cmd)),
      },
      pending' = pending.set(h, []),
      fixedConfig, lane' = lane, substrate' = substrate, cursor' = cursor, hwm' = hwm,
    }
  }

  // --- Fault: at-least-once redelivery of a claimed row ----------------------
  action redeliver = {
    nondet c = ran.oneOf()
    val h = owner.get(c.proc)
    all {
      ran != Set(),
      not(lane.get(h).indices().exists(i => lane.get(h)[i].id == c.id)),
      not(pending.get(h).indices().exists(i => pending.get(h)[i].id == c.id)),
      lane' = lane.set(h, [c].concat(lane.get(h))),
      fixedConfig, log' = log, substrate' = substrate, cursor' = cursor, hwm' = hwm, pending' = pending, ran' = ran,
    }
  }

  // --- Fault: cursor loss (commit-then-save: models keep, position rewinds) ---
  action rewind = {
    nondet h = GROUPS.oneOf()
    nondet back = 0.to(MAX_LOG).oneOf()
    all {
      back < cursor.get(h),
      cursor' = cursor.set(h, back),
      g' = g, guarded' = guarded, owner' = owner, submitted' = submitted, rewound' = true,
      log' = log, substrate' = substrate, hwm' = hwm, lane' = lane, pending' = pending, ran' = ran,
    }
  }

  val allIdle = GROUPS.forall(h => lane.get(h).length() == 0 and pending.get(h).length() == 0 and cursor.get(h) == log.length())
  action quiescent = all {
    submitted == MAX_SUBMISSIONS,
    allIdle or log.length() >= MAX_LOG,
    fixedConfig, log' = log, substrate' = substrate, cursor' = cursor, hwm' = hwm, lane' = lane, pending' = pending, ran' = ran,
  }

  action step = any { submit, foldOne, claim, execute, redeliver, rewind, quiescent }

  // --- Placement predicates (topology.md) ------------------------------------
  // Rule 6: a reader shares a group with every model it reads.
  val rule6 = ELEMS.forall(e => g.reads.get(e).forall(m => owner.get(m) == owner.get(e)))
  // The degenerate topology: everything in one group (st as a topology).
  val oneGroup = GROUPS.exists(h => PLACEABLE.forall(x => owner.get(x) == h))

  // --- Invariants for EVERY topology --------------------------------------------

  // Each model is exactly its owner's fold up to what the owner has really folded.
  val substrateIsFold =
    MODELS.forall(m => substrate.get(m) == foldPrefix(g, log, hwm.get(owner.get(m))).get(m))
  val cursorBelowHwm = GROUPS.forall(h => cursor.get(h) <= hwm.get(h))

  val blocksWellFormed = blocksWellFormedOf(g, log)
  // Exactly-once needs BOTH the application guard and rule 6: a guard sees its
  // own block only through a model its own group folds before it claims.
  val effectsOnce = (guarded and rule6) implies effectsOnceGuardableOf(g, log)
  val causal = causalOf(g, log)

  // A command only ever sits in the lane / pending slot of its procedure's owner.
  val routed = GROUPS.forall(h =>
    lane.get(h).indices().forall(i => owner.get(lane.get(h)[i].proc) == h)
    and pending.get(h).indices().forall(i => owner.get(pending.get(h)[i].proc) == h))

  // Completeness: every reacting policy of every entry its owner has folded
  // has dispatched (queued, claimed, or run); every run left its block.
  val knownIds =
    GROUPS.map(h => lane.get(h).indices().map(i => lane.get(h)[i].id).union(pending.get(h).indices().map(i => pending.get(h)[i].id))).flatten()
      .union(ran.map(c => c.id))
  val dispatchComplete =
    log.indices().forall(pos =>
      POLICIES.forall(y => (g.reacts.get(y).contains(log[pos].kind) and pos < hwm.get(owner.get(y))) implies
        g.dispatches.get(y).forall(p => knownIds.contains({ origin: y, at: pos, target: p }))))
  val emitComplete =
    ran.forall(c => g.emits.get(c.proc) == Set() or log.indices().exists(i => log[i].cause == c.id))

  // --- The theorems ----------------------------------------------------------------
  val freshReads          = freshReadsOf(g, log)
  val freshDispatch       = freshDispatchOf(g, log)
  val causeFolded         = causeFoldedOf(g, log)
  val dispatchCauseFolded = dispatchCauseFoldedOf(g, log)

  // (T1) What the worker promises, made conditional on rule 6: the model had
  //      committed through the command's cause before the run, and no policy
  //      dispatched on a view behind its trigger. NOT freshDispatch: after a
  //      cursor rewind a policy legitimately re-runs on a model that has
  //      committed further (cursors.py commit-then-save), and its re-dispatch
  //      only reaches the log when the application does not, or cannot, guard.
  val rule6Guarantee = rule6 implies (causeFolded and dispatchCauseFolded)

  // (T1') In the no-fault world the strong form does hold.
  val noFaultFreshDispatch = (rule6 and not(rewound)) implies freshDispatch

  // (T2) st falls out as a topology: one group ⇒ st's fresh reads.
  val singleGroupIsSt = oneGroup implies (freshReads and dispatchCauseFolded and (not(rewound) implies freshDispatch))

  val allInvariants = all {
    substrateIsFold, cursorBelowHwm, blocksWellFormed, effectsOnce, causal, routed,
    dispatchComplete, emitComplete, rule6Guarantee, noFaultFreshDispatch, singleGroupIsSt
  }

  // --- Witnesses ---------------------------------------------------------------------
  // The claim→execute window: a rule-6-LEGAL split reads behind the tail.
  // (May still be serializable — a block reorder between claim and execute.)
  val witnessClaimWindow  = rule6 and not(freshReads)
  // Write skew under rule 6: two decisions, neither saw the other, no serial
  // order explains both. THE counter-example to "byte-identical models".
  val witnessWriteSkew    = rule6 and writeSkewOf(g, log)
  // A rule-6-VIOLATING split breaks even the weak guarantee.
  val witnessStaleRead    = not(rule6) and not(causeFolded)
  val witnessStalePolicy  = not(rule6) and not(freshDispatch)
  val witnessLegalLag     = rule6 and not(oneGroup) and GROUPS.exists(h => cursor.get(h) < log.length()) and log.length() >= 2
  val witnessCrossGroup   = log.indices().exists(i => log[i].cause.origin != "host" and owner.get(log[i].cause.origin) != owner.get(log[i].cause.target))
  val witnessRedelivered  = GROUPS.exists(h => lane.get(h).indices().exists(i => ran.exists(c => c.id == lane.get(h)[i].id)))
  val witnessRewound      = GROUPS.exists(h => cursor.get(h) < hwm.get(h))
  val witnessRedispatch   = witnessRewound and GROUPS.exists(h => lane.get(h).indices().exists(i => ran.exists(c => c.id == lane.get(h)[i].id)))
  val witnessDoubleAppend = not(effectsOnceOf(log))
  val witnessUnguardable  = guarded and rule6 and not(effectsOnceOf(log))
  // A guarding, guardable procedure still double-appends when rule 6 is broken.
  val witnessGuardMissed  = guarded and not(rule6) and not(effectsOnceGuardableOf(g, log))
  val witnessOneGroupRun  = oneGroup and log.length() >= 2
}
