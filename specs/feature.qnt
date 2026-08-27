// specs/feature.qnt
//
// An *abstract* Dizzy feature, small enough to model-check, shaped so that
// every element kind and every placement-relevant distinction appears once:
//
//   command  register(name)   → procedure `register`   (READS model `names`)
//                                 emits registered(name) | rejected(name)
//   event    registered       → projection `names_proj` folds into model `names`
//   event    registered       → policy `welcome` (reads nothing)
//                                 dispatches send_welcome(name)
//   command  send_welcome     → procedure `send_welcome` (reads nothing)
//                                 emits welcomed(name)
//
// `register` is the non-monotone reader (a uniqueness check): rule 3 pins it
// to one lane and rule 6 pins it to `names_proj`'s group. `send_welcome` and
// `welcome` read nothing and may be placed anywhere. That is the whole
// vocabulary the engine specs need.
//
// Engines import this module; nothing here is engine-specific.

module feature {

  pure val NAMES = Set("alice", "bob")

  // --- Commands ---------------------------------------------------------
  // `id` is the deterministic id the queue dedupes on. Externally submitted
  // commands get a fresh id from the host; policy-dispatched commands derive
  // theirs from (trigger event position, policy) so a re-run no-ops.
  // No string concatenation in Quint, so an id is a structured value:
  //   host-submitted:      { origin: "host",     at: <submission counter> }
  //   policy-dispatched:   { origin: <policy>,   at: <trigger event position> }
  type CmdId = { origin: str, at: int }
  type Command = { id: CmdId, kind: str, name: str }

  // --- Events -----------------------------------------------------------
  // `cause` is the id of the command whose procedure emitted it; it is what
  // lets a spec state causality without hashes.
  type Event = { kind: str, name: str, cause: CmdId }

  // --- Models -----------------------------------------------------------
  // One model, `names`: the set of registered names. Represented as the
  // state of a fold so engines can carry it as a variable and compare
  // it to a refold of the log (the convergence invariant).
  type Models = { names: Set[str] }
  pure val emptyModels: Models = { names: Set() }

  // --- Projections: models ← fold(events) -------------------------------
  pure def applyEvent(m: Models, e: Event): Models =
    if (e.kind == "registered") { names: m.names.union(Set(e.name)) }
    else m

  pure def foldModels(events: List[Event]): Models =
    events.foldl(emptyModels, (m, e) => applyEvent(m, e))

  // --- Procedures: events ← procedure(command, models) --------------------
  // Returned as a list so a procedure may emit zero or many events.
  pure def runProcedure(cmd: Command, m: Models): List[Event] =
    if (cmd.kind == "register")
      if (m.names.contains(cmd.name))
        [{ kind: "rejected",   name: cmd.name, cause: cmd.id }]
      else
        [{ kind: "registered", name: cmd.name, cause: cmd.id }]
    else if (cmd.kind == "send_welcome")
      [{ kind: "welcomed", name: cmd.name, cause: cmd.id }]
    else []

  // Which model(s) a procedure reads — the rule-3 / rule-6 discriminator.
  pure def procedureReads(kind: str): Set[str] =
    if (kind == "register") Set("names") else Set()

  // --- Policies: commands ← policy(event) ---------------------------------
  // `pos` is the event's log position; it seeds the deterministic id.
  pure def runPolicies(e: Event, pos: int): List[Command] =
    if (e.kind == "registered")
      [{ id: { origin: "welcome", at: pos }, kind: "send_welcome", name: e.name }]
    else []

  pure def policyReads(e: Event): Set[str] = Set()
}
