#!/usr/bin/env python3
"""Run every spec check and write report/results.json (+ per-check logs).

Usage: python3 report/run_checks.py [--quick] [--no-verify]
Each check is (spec, kind, target, expectation). `witness` checks run
`--invariant=not(X)` and EXPECT a violation — it proves X is reachable.
"""
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "specs"
OUT = ROOT / "report"
LOGS = OUT / "logs"
LOGS.mkdir(parents=True, exist_ok=True)
QUINT = shutil.which("quint") or os.path.expanduser("~/.bun/bin/quint")
QUICK = "--quick" in sys.argv
NO_VERIFY = "--no-verify" in sys.argv
SAMPLES = "300" if QUICK else "8000"

CHECKS = [
  # spec, kind, target, expect, note
  # engine.qnt — the topology engine as shipped (engine/src/dizzy_topology_engine).
  ("engine.qnt", "invariant", "modelsAreFold",    "ok", "E1 the read model is a fold of the log, across crashes (refold on open)"),
  ("engine.qnt", "invariant", "cursorSound",      "ok", "E2 the cursor never claims ground the read model has not covered"),
  ("engine.qnt", "invariant", "blocksWellFormed", "ok", "E3 every run leaves one complete, contiguous, ordered block"),
  ("engine.qnt", "invariant", "effectsOnce",      "ok", "E4 EXACTLY-ONCE effects, unconditionally: append and ack are one transaction"),
  ("engine.qnt", "invariant", "ackAtomic",        "ok", "E5 a block's command is done; a done command has its block; nothing else appended"),
  ("engine.qnt", "invariant", "causal",           "ok", "E6 policy-dispatched effects follow a trigger the policy reacts to"),
  ("engine.qnt", "invariant", "freshReads",       "ok", "E7 with rule 3, every procedure read the fold of everything before its block — pools included"),
  ("engine.qnt", "invariant", "freshDispatch",    "ok", "E8 every dispatching policy read exactly the fold through its trigger, across crashes"),
  ("engine.qnt", "invariant", "freshQueue",       "ok", "E8' the same, for commands still queued"),
  ("engine.qnt", "invariant", "dispatchComplete", "ok", "E9 every settled event's reacting policies have dispatched"),
  ("engine.qnt", "invariant", "idsDistinct",      "ok", "E10 one row per command id (INSERT OR IGNORE)"),
  ("engine.qnt", "invariant", "memorySound",      "ok", "E11 the host's in-flight units are rows really running; pools within their width"),
  ("engine.qnt", "invariant", "noLostWork",       "ok", "E12 an idle engine has finished every command"),
  ("engine.qnt", "witness",   "witnessCascade",          "violation", "a policy-dispatched command ran"),
  ("engine.qnt", "witness",   "witnessReader",           "violation", "a procedure observed a non-empty model"),
  ("engine.qnt", "witness",   "witnessPooledRun",        "violation", "a pooled procedure's block was committed"),
  ("engine.qnt", "witness",   "witnessConcurrent",       "violation", "the pool lane was full: jobs genuinely in flight together"),
  ("engine.qnt", "witness",   "witnessOutOfOrder",       "violation", "under rule 3 the log left claim order — and every read was still fresh"),
  ("engine.qnt", "witness",   "witnessPriority",         "violation", "an interactive command was claimed ahead of an older batch one"),
  ("engine.qnt", "witness",   "witnessRecovered",        "violation", "an interrupted attempt re-ran under its own id after a crash"),
  ("engine.qnt", "witness",   "witnessCrashAfterAppend", "violation", "a crash after an append"),
  ("engine.qnt", "witness",   "witnessResettle",         "violation", "a crash between enqueue and cursor; the re-settle's ids were ignored"),
  ("engine.qnt", "witness",   "witnessFailed",           "violation", "an element raised; its attempt failed and appended nothing"),
  ("engine.qnt", "witness",   "witnessRetried",          "violation", "a failure was retried by the host and the run finished clean"),
  ("engine.qnt", "witness",   "witnessBound",            "violation", "the log bound was reached (cascades are bounded, not absent)"),
  ("engine.qnt", "witness",   "witnessStalePool",        "violation", "rule 3 is NECESSARY: a pooled reader decided on a stale model"),
  ("engine.qnt", "verify",    "allInvariants",           "ok", "Apalache: all invariants, every graph × placement, 5 steps"),

  ("st.qnt", "invariant", "zeroLag",          "ok", "I1 no lag: cursor == log.length()"),
  ("st.qnt", "invariant", "modelsAreFold",    "ok", "I2 carried models are a refold of the log (the rebuild guarantee)"),
  ("st.qnt", "invariant", "blocksWellFormed", "ok", "I3 every run leaves one complete, contiguous, ordered block"),
  ("st.qnt", "invariant", "effectsOnce",      "ok", "I3' exactly-once effects for GUARDABLE procedures, when the application guards"),
  ("st.qnt", "invariant", "causal",           "ok", "I4 policy-dispatched effects follow a trigger the policy reacts to"),
  ("st.qnt", "invariant", "freshReads",       "ok", "I5 every procedure read the fold of everything before its block"),
  ("st.qnt", "invariant", "freshDispatch",    "ok", "I6 every dispatching policy read the fold through its trigger"),
  ("st.qnt", "invariant", "freshQueue",       "ok", "I6' the same, for commands still queued"),
  ("st.qnt", "invariant", "dispatchComplete", "ok", "I7 every reacting policy of every event has dispatched"),
  ("st.qnt", "invariant", "emitComplete",     "ok", "I7' every command that ran left its block"),
  ("st.qnt", "invariant", "queueIdsDistinct", "ok", "I8 one DB row per command"),
  ("st.qnt", "witness",   "witnessCascade",      "violation", "a policy-dispatched command ran"),
  ("st.qnt", "witness",   "witnessReader",       "violation", "a procedure observed a non-empty model"),
  ("st.qnt", "witness",   "witnessPolicyReader", "violation", "a policy observed a non-empty model"),
  ("st.qnt", "witness",   "witnessPriority",     "violation", "an interactive command was claimed ahead of the queue head"),
  ("st.qnt", "witness",   "witnessBound",        "violation", "the log bound was reached (cascades are bounded, not absent)"),
  ("st.qnt", "witness",   "witnessRedelivered",  "violation", "a claimed row was recovered and re-queued"),
  ("st.qnt", "witness",   "witnessGuardNoop",    "violation", "a guarding procedure saw its own block and no-op'd its redelivery"),
  ("st.qnt", "witness",   "witnessUnguardable",  "violation", "a guarding application still double-appended: the procedure was not guardable"),
  ("st.qnt", "witness",   "witnessDoubleAppend", "violation", "an unguarded redelivery appended a second block (executor.py:49-56)"),
  ("st.qnt", "verify",    "allInvariants",    "ok", "Apalache: all invariants, every graph × guard, 4 steps"),

  ("groups.qnt", "invariant", "substrateIsFold",  "ok", "each model == its owner's fold up to the owner's high-water mark"),
  ("groups.qnt", "invariant", "cursorBelowHwm",   "ok", "a rewound cursor never exceeds what was folded"),
  ("groups.qnt", "invariant", "blocksWellFormed", "ok", "as st, under any topology"),
  ("groups.qnt", "invariant", "effectsOnce",      "ok", "exactly-once for guardable procedures needs the guard AND rule 6"),
  ("groups.qnt", "invariant", "causal",           "ok", "as st, under any topology"),
  ("groups.qnt", "invariant", "routed",           "ok", "a command sits only in its procedure's owner lane / pending slot"),
  ("groups.qnt", "invariant", "dispatchComplete", "ok", "every folded event's reacting policies have dispatched"),
  ("groups.qnt", "invariant", "emitComplete",     "ok", "every command that ran left its block"),
  ("groups.qnt", "invariant", "rule6Guarantee",       "ok", "T1: rule 6 ⇒ model committed through the cause before the run; no policy dispatched behind its trigger"),
  ("groups.qnt", "invariant", "noFaultFreshDispatch", "ok", "T1': with rule 6 and no cursor loss, policies read exactly the fold through their trigger"),
  ("groups.qnt", "invariant", "singleGroupIsSt",      "ok", "T2: one group ⇒ st's fresh reads"),
  ("groups.qnt", "witness",   "witnessClaimWindow",  "violation", "a rule-6-LEGAL split read behind the tail (the claim→execute window, groups.py:44-50)"),
  ("groups.qnt", "witness",   "witnessWriteSkew",    "violation", "WRITE SKEW under rule 6: two decisions, neither saw the other — no serial order explains both"),
  ("groups.qnt", "witness",   "witnessStaleRead",    "violation", "a rule-6-violating split broke even the weak guarantee"),
  ("groups.qnt", "witness",   "witnessStalePolicy",  "violation", "a rule-6-violating policy read stale or ahead"),
  ("groups.qnt", "witness",   "witnessLegalLag",     "violation", "a legal multi-group split with real inter-group lag"),
  ("groups.qnt", "witness",   "witnessCrossGroup",   "violation", "a policy in one group dispatched to a procedure in another"),
  ("groups.qnt", "witness",   "witnessRedelivered",  "violation", "an at-least-once redelivery occurred"),
  ("groups.qnt", "witness",   "witnessRewound",      "violation", "a cursor was lost and rewound"),
  ("groups.qnt", "witness",   "witnessRedispatch",   "violation", "a rewound cursor re-dispatched an already-run command"),
  ("groups.qnt", "witness",   "witnessDoubleAppend", "violation", "an unguarded replay appended a second block"),
  ("groups.qnt", "witness",   "witnessUnguardable",  "violation", "a guarding application still double-appended: procedure not guardable"),
  ("groups.qnt", "witness",   "witnessGuardMissed",  "violation", "a guarding, guardable procedure double-appended because rule 6 was broken"),
  ("groups.qnt", "witness",   "witnessOneGroupRun",  "violation", "the one-group (st) topology was exercised"),
  ("groups.qnt", "verify",    "allInvariants",      "ok", "Apalache: all invariants, every graph × placement × guard, 3 steps"),
]

def run(spec, kind, target):
  if kind == "verify":
    depth = {"st.qnt": "4", "engine.qnt": "5"}.get(spec, "3")
    cmd = [QUINT, "verify", spec, f"--invariant={target}", f"--max-steps={depth}"]
    timeout = 3000
  else:
    inv = target if kind == "invariant" else f"not({target})"
    steps = "25" if spec == "engine.qnt" else "20"   # crash + re-settle need the extra steps
    cmd = [QUINT, "run", spec, f"--invariant={inv}", f"--max-samples={SAMPLES}", f"--max-steps={steps}", "--seed=7"]
    timeout = 900
  t0 = time.time()
  try:
    p = subprocess.run(cmd, cwd=SPECS, capture_output=True, text=True, timeout=timeout)
    out = p.stdout + p.stderr
  except subprocess.TimeoutExpired:
    out = "TIMEOUT"
  secs = round(time.time() - t0, 1)
  m = re.search(r"^\[(ok|violation)\]", out, re.M)
  result = m.group(1) if m else ("deadlock" if "deadlock" in out else "error")
  log = LOGS / f"{spec}.{kind}.{target}.log"
  log.write_text(" ".join(cmd) + "\n\n" + out)
  # keep the last state of a counterexample for the report
  trace = None
  if result == "violation" and kind != "witness":
    trace = out[-6000:]
  return result, secs, str(log.relative_to(OUT)), trace

results = []
for spec, kind, target, expect, note in CHECKS:
  if NO_VERIFY and kind == "verify":
    continue
  result, secs, log, trace = run(spec, kind, target)
  passed = (result == expect)
  print(f"{'PASS' if passed else 'FAIL'}  {spec:12} {kind:9} {target:20} {result:9} {secs}s", flush=True)
  results.append(dict(spec=spec, kind=kind, target=target, expect=expect, result=result,
                      passed=passed, seconds=secs, note=note, log=log, trace=trace))

meta = dict(
  generated=time.strftime("%Y-%m-%d %H:%M:%S"),
  quint=subprocess.run([QUINT, "--version"], capture_output=True, text=True).stdout.strip(),
  samples=SAMPLES,
  dizzy=subprocess.run(["git", "-C", str(ROOT.parent / "ext" / "dizzy"), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip(),
)
(OUT / "results.json").write_text(json.dumps(dict(meta=meta, checks=results), indent=2))
print(f"\n{sum(r['passed'] for r in results)}/{len(results)} passed → report/results.json")
