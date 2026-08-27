// specs/graph.qnt — the abstract shape of a Dizzy feature.
//
// The engine never interprets a command, an event or a model; it only routes
// them. So a feature, at the engine's altitude, is a *graph*:
//
//   handles   : each procedure handles exactly one command kind (the command
//               kind IS the procedure id here — nothing is lost by that)
//   reads     : element (procedure | policy) → models it declares `queries:` on
//   emits     : procedure → event kinds it emits
//   folds     : model → event kinds its projections fold
//   reacts    : policy → event kinds it reacts to
//   dispatches: policy → procedures (command kinds) it dispatches
//   interactive: procedures whose command kind is `priority: interactive`
//
// No names, no payloads, no business logic. Model *contents* are opaque to the
// engine, so a model is represented by the SET OF LOG POSITIONS it has folded,
// and a procedure's output is an event tagged with the VIEW it read — the
// folded positions of every model it reads. Two topologies produce
// byte-identical models iff every reader saw the same view, which is exactly
// the property topology.md asks for, stated without semantics.
//
// A graph is chosen nondeterministically at init over a bounded universe, so a
// bounded model-check is a check over EVERY feature of that size — not over an
// example.

module graph {

  // --- Bounded universe --------------------------------------------------
  pure val PROCS    = Set("p1", "p2")        // procedures == command kinds
  pure val POLICIES = Set("y1")
  pure val MODELS   = Set("m1", "m2")
  pure val KINDS    = Set("e1", "e2")        // event kinds
  pure val ELEMS    = PROCS.union(POLICIES)  // things that may declare `queries:`
  // The same universes as lists, for deterministic iteration (Quint sets are unordered).
  pure val PROC_LIST   = ["p1", "p2"]
  pure val POLICY_LIST = ["y1"]
  pure val KIND_LIST   = ["e1", "e2"]

  type Graph = {
    reads:      str -> Set[str],   // ELEMS    → ⊆ MODELS
    emits:      str -> Set[str],   // PROCS    → ⊆ KINDS
    folds:      str -> Set[str],   // MODELS   → ⊆ KINDS
    reacts:     str -> Set[str],   // POLICIES → ⊆ KINDS
    dispatches: str -> Set[str],   // POLICIES → ⊆ PROCS
    interactive: Set[str],         // ⊆ PROCS
  }

  // The candidate sets for each relation. An engine's `init` draws one of each
  // with `nondet` — a bounded check therefore ranges over EVERY graph of this
  // size (|graphs| = 4^3·4^2·4^2·4·4·4 ≈ 10^6 for the universe above).
  pure val READS      = ELEMS.setOfMaps(MODELS.powerset())
  pure val EMITS      = PROCS.setOfMaps(KINDS.powerset())
  pure val FOLDS      = MODELS.setOfMaps(KINDS.powerset())
  pure val REACTS     = POLICIES.setOfMaps(KINDS.powerset())
  pure val DISPATCHES = POLICIES.setOfMaps(PROCS.powerset())
  pure val INTERACTIVE = PROCS.powerset()

  // --- Runtime vocabulary ------------------------------------------------
  // A view: for each model, the log positions folded into it.
  type View = str -> Set[int]
  pure val emptyView: View = MODELS.mapBy(m => Set())

  // A command id is deterministic: host-submitted ids are numbered by the
  // host; policy-dispatched ids are (policy, trigger position, target).
  type CmdId = { origin: str, at: int, target: str }
  type Command = { id: CmdId, proc: str, view: View }   // view = what the dispatching policy read
  // An event carries BOTH views on its causal path, so log-level invariants can
  // see a stale policy read as well as a stale procedure read.
  // `at` is the log position where this run's block starts — stamped by the
  // engine at append time, it is what tells two runs of one cause apart.
  type Event   = { kind: str, cause: CmdId, at: int, view: View, dispatchView: View }

  // --- Semantics that are NOT feature-specific ----------------------------

  // Fold one event at position `pos` into the models of `g`.
  pure def applyEvent(g: Graph, v: View, e: Event, pos: int): View =
    MODELS.mapBy(m =>
      if (g.folds.get(m).contains(e.kind)) v.get(m).union(Set(pos)) else v.get(m))

  // The view a fold of log[0, upto) produces — the reference "truth".
  pure def foldPrefix(g: Graph, log: List[Event], upto: int): View =
    MODELS.mapBy(m =>
      log.indices().filter(i => i < upto and g.folds.get(m).contains(log[i].kind)))

  // Restrict a view to the models an element reads (what it could observe).
  pure def restrict(g: Graph, elem: str, v: View): View =
    MODELS.mapBy(m => if (g.reads.get(elem).contains(m)) v.get(m) else Set())

  // Run a procedure: one event per kind it emits (in KIND_LIST order), each
  // tagged with its view. Total and deterministic in (command, view): the
  // data-dependence of a real procedure is captured by the tag, not by branching.
  pure def runProcedure(g: Graph, cmd: Command, v: View, at: int): List[Event] =
    KIND_LIST.foldl([], (acc, k) =>
      if (g.emits.get(cmd.proc).contains(k))
        acc.append({ kind: k, cause: cmd.id, at: at, view: restrict(g, cmd.proc, v), dispatchView: cmd.view })
      else acc)

  // Run every policy of `g` that reacts to event `e` at position `pos`,
  // reading view `v`. Yields the commands they dispatch, ordered (policy, target).
  pure def runPolicies(g: Graph, e: Event, pos: int, v: View): List[Command] =
    POLICY_LIST.foldl([], (acc, y) =>
      if (g.reacts.get(y).contains(e.kind))
        PROC_LIST.foldl(acc, (acc2, p) =>
          if (g.dispatches.get(y).contains(p))
            acc2.append({ id: { origin: y, at: pos, target: p }, proc: p, view: restrict(g, y, v) })
          else acc2)
      else acc)

  // --- Rule vocabulary (topology.md) ----------------------------------------
  pure def isReader(g: Graph, elem: str): bool = g.reads.get(elem) != Set()
  pure def readersOf(g: Graph): Set[str] = ELEMS.filter(e => isReader(g, e))

  // --- Log vocabulary shared by every engine spec ---------------------------
  // A "block" is a maximal run of equal-cause events: one procedure run.
  // With at-least-once delivery the same cause may own several blocks.
  pure def blockStart(l: List[Event], i: int): int = l[i].at

  // Every block is a complete, well-ordered run of its procedure.
  pure def blocksWellFormedOf(g: Graph, l: List[Event]): bool =
    l.indices().forall(i => {
      val b = blockStart(l, i)
      val kinds = g.emits.get(l[i].cause.target)
      // block length == |emits|, and its kinds are exactly emits, in KIND_LIST order
      val blockIdx = l.indices().filter(j => blockStart(l, j) == b)
      all {
        b <= i,
        blockIdx.size() == kinds.size(),
        blockIdx.map(j => l[j].kind) == kinds,
        blockIdx.forall(j => l[j].cause == l[i].cause),
        // contiguous: the block occupies [b, b + |kinds|)
        blockIdx.forall(j => j < b + kinds.size()),
      }
    })

  // The view a procedure read is the fold of everything before ITS block,
  // restricted to what it declares — the sequential-consistency statement.
  pure def freshReadsOf(g: Graph, l: List[Event]): bool =
    l.indices().forall(i =>
      l[i].view == restrict(g, l[i].cause.target, foldPrefix(g, l, blockStart(l, i))))

  // The dispatching policy read the fold through its trigger, restricted.
  pure def freshDispatchOf(g: Graph, l: List[Event]): bool =
    l.indices().forall(i => {
      val c = l[i].cause
      c.origin != "host" implies
        l[i].dispatchView == restrict(g, c.origin, foldPrefix(g, l, c.at + 1))
    })

  // The weaker guarantee groups.py actually states: the read model had
  // committed THROUGH THE COMMAND'S CAUSE before the procedure ran
  // (view ⊇ fold-through-trigger). Host commands have no cause: vacuous.
  pure def causeFoldedOf(g: Graph, l: List[Event]): bool =
    l.indices().forall(i => {
      val c = l[i].cause
      c.origin != "host" implies
        MODELS.forall(m =>
          restrict(g, c.target, foldPrefix(g, l, c.at + 1)).get(m).subseteq(l[i].view.get(m)))
    })

  // Causality of ids: a non-host cause names a real trigger before it.
  pure def causalOf(g: Graph, l: List[Event]): bool =
    l.indices().forall(i => {
      val c = l[i].cause
      c.origin != "host" implies all {
        c.at < i,
        g.reacts.get(c.origin).contains(l[c.at].kind),
        g.dispatches.get(c.origin).contains(c.target),
      }
    })

  // A procedure can dedupe its own redelivery ONLY through what it can see:
  // its read models. It is "guardable" iff it reads a model that folds one of
  // the kinds it emits (and events carry their cause). This is the rule
  // topology.md:347-351 ("procedures guard") silently assumes.
  pure def guardable(g: Graph, p: str): bool =
    g.reads.get(p).exists(m => g.folds.get(m).intersect(g.emits.get(p)) != Set())

  // The implementable guard: "my own earlier block is already in a model I read".
  pure def seesOwnBlock(g: Graph, l: List[Event], v: View, c: Command): bool =
    g.reads.get(c.proc).exists(m => v.get(m).exists(pos => l[pos].cause == c.id))

  // Exactly-once effects, for the procedures that CAN guard.
  pure def effectsOnceGuardableOf(g: Graph, l: List[Event]): bool =
    l.indices().forall(i => l.indices().forall(j =>
      (l[i].cause == l[j].cause and guardable(g, l[i].cause.target)) implies blockStart(l, i) == blockStart(l, j)))

  // A dispatching policy never saw a view BEHIND its trigger (the ⊇ form of
  // freshDispatch; survives replay after cursor loss).
  pure def dispatchCauseFoldedOf(g: Graph, l: List[Event]): bool =
    l.indices().forall(i => {
      val c = l[i].cause
      c.origin != "host" implies
        MODELS.forall(m => restrict(g, c.origin, foldPrefix(g, l, c.at + 1)).get(m).subseteq(l[i].dispatchView.get(m)))
    })

  // Write skew: two blocks of different causes, each a reader of a model that
  // folds the other's kind, and neither saw the other. No serial order
  // reproduces both views — the counter-example to "rule 6 removes decisions
  // reached against a stale model".
  pure def writeSkewOf(g: Graph, l: List[Event]): bool =
    l.indices().exists(i => l.indices().exists(j => all {
      l[i].cause != l[j].cause,
      MODELS.exists(m => g.reads.get(l[i].cause.target).contains(m) and g.folds.get(m).contains(l[j].kind) and not(l[i].view.get(m).contains(j))),
      MODELS.exists(m => g.reads.get(l[j].cause.target).contains(m) and g.folds.get(m).contains(l[i].kind) and not(l[j].view.get(m).contains(i))),
    }))

  // Each cause owns exactly one block — exactly-once effects.
  pure def effectsOnceOf(l: List[Event]): bool =
    l.indices().forall(i => l.indices().forall(j =>
      (l[i].cause == l[j].cause) implies blockStart(l, i) == blockStart(l, j)))
}
