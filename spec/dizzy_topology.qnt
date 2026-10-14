// dizzy_topology.qnt
//
// A minimal spec of Dizzy's async-first semantics:
//   - one append-only event log (the only shared truth)
//   - command handlers whose projections settle with arbitrary lag
//   - a non-monotone validation (unique usernames) and a client query
//
// Two step relations model two topologies:
//   stepUnsafe : every component fully independent, no coordination
//   stepSafe   : uniqueness check co-located with the append (aggregate
//                boundary) + watermark-guarded queries
//
// Try:
//   quint run dizzy_topology.qnt --init=init --step=stepUnsafe --invariant=uniqueNames
//   quint run dizzy_topology.qnt --init=init --step=stepUnsafe --invariant=readYourWrites
//   quint run dizzy_topology.qnt --init=init --step=stepSafe   --invariant=uniqueNames
//   quint run dizzy_topology.qnt --init=init --step=stepSafe   --invariant=readYourWrites
//   quint verify dizzy_topology.qnt --init=init --step=stepSafe --invariant=uniqueNames

module dizzy_topology {

  // === The universe (kept tiny on purpose) ===
  pure val NAMES = Set("alice", "bob")
  pure val COMPONENTS = Set("c1", "c2")

  // === Shared truth: the event log ===
  type Event = { name: str }
  var log: List[Event]

  // === Per-component projection state ===
  // applied.get(c) = how far c's model has settled into the log (its lag)
  var applied: str -> int

  // === Client-side state, for read-your-writes ===
  var issuedAt: int       // log position just past the client's last command; -1 = none
  var observed: Set[str]  // what the client's last query returned
  var queried: bool

  // The model derived from the log prefix [0, p) — i.e. a projection
  // that has settled up to position p.
  pure def modelAt(l: List[Event], p: int): Set[str] =
    l.indices().filter(i => i < p).map(i => l[i].name)

  action init = all {
    log' = [],
    applied' = COMPONENTS.mapBy(c => 0),
    issuedAt' = -1,
    observed' = Set(),
    queried' = false,
  }

  // --- Projections settle asynchronously, one event at a time ---
  action settle = {
    nondet c = COMPONENTS.oneOf()
    all {
      applied.get(c) < log.length(),
      applied' = applied.set(c, applied.get(c) + 1),
      log' = log, issuedAt' = issuedAt, observed' = observed, queried' = queried,
    }
  }

  // --- UNSAFE: validate the command against a possibly-stale model ---
  // Any replica may accept a registration if *its* settled view says
  // the name is free. Non-monotone check + lag = duplicates.
  action registerStale = {
    nondet c = COMPONENTS.oneOf()
    nondet n = NAMES.oneOf()
    all {
      not(modelAt(log, applied.get(c)).contains(n)),
      log' = log.append({ name: n }),
      issuedAt' = log.length() + 1,
      applied' = applied, observed' = observed, queried' = false,
    }
  }

  // --- SAFE: the uniqueness check is non-monotone, so it must live in
  // the same consistency boundary as the append (the DDD aggregate).
  // Modeled as validate-at-head + append in one atomic action.
  action registerAtomic = {
    nondet n = NAMES.oneOf()
    all {
      not(modelAt(log, log.length()).contains(n)),
      log' = log.append({ name: n }),
      issuedAt' = log.length() + 1,
      applied' = applied, observed' = observed, queried' = false,
    }
  }

  // --- Query without a watermark: read whatever has settled anywhere ---
  action queryStale = {
    nondet c = COMPONENTS.oneOf()
    all {
      issuedAt >= 0,
      observed' = modelAt(log, applied.get(c)),
      queried' = true,
      log' = log, applied' = applied, issuedAt' = issuedAt,
    }
  }

  // --- Query with a watermark token: the command returned its log
  // position; the query blocks until some replica has settled past it.
  action queryWatermarked = {
    nondet c = COMPONENTS.oneOf()
    all {
      issuedAt >= 0,
      applied.get(c) >= issuedAt,     // the watermark guard
      observed' = modelAt(log, applied.get(c)),
      queried' = true,
      log' = log, applied' = applied, issuedAt' = issuedAt,
    }
  }

  // === Topologies as step relations ===
  action stepUnsafe = any { settle, registerStale, queryStale }
  action stepSafe   = any { settle, registerAtomic, queryWatermarked }

  // === Properties every legal topology must preserve ===

  // 1. Uniqueness: no name is ever registered twice.
  val uniqueNames =
    log.indices().forall(i =>
      log.indices().forall(j =>
        (i != j) implies (log[i].name != log[j].name)))

  // 2. Read-your-writes: a completed query reflects everything up to
  // the client's own last command.
  val readYourWrites =
    (queried and issuedAt >= 0) implies
      modelAt(log, issuedAt).subseteq(observed)
}
