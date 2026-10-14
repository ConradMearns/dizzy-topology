#!/usr/bin/env python3
"""Scale ladder: generate each size, time typecheck / simulation / Apalache, record RSS.

Usage: python3 scale/bench.py [--samples N] [--rungs a,b,c] [--no-verify]
Writes scale/results.json incrementally (one row per rung × spec) so a killed run keeps its data.
"""
import json, os, re, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen import generate, ROOT

QUINT = os.path.expanduser("~/.bun/bin/quint")
OUT = ROOT / "scale" / "results.json"
args = sys.argv[1:]
SAMPLES = int(args[args.index("--samples") + 1]) if "--samples" in args else 300
NO_VERIFY = "--no-verify" in args
STEPS = 20

#        tag       procs policies models kinds groups max_log  verify_depth
LADDER = [
    ("p2",    2,   1,  2,  2, 2,  6, 3),
    ("p5",    5,   2,  3,  3, 2,  8, 2),
    ("p10",  10,   4,  4,  4, 3,  8, 1),
    ("p25",  25,   8,  6,  4, 3, 10, 0),
    ("p50",  50,  16,  8,  6, 4, 10, 0),
    ("p100", 100, 32, 12,  8, 4, 12, 0),
    ("p200", 200, 64, 16, 10, 6, 12, 0),
    ("p400", 400, 128, 24, 12, 8, 12, 0),
]
if "--rungs" in args:
    keep = set(args[args.index("--rungs") + 1].split(","))
    LADDER = [r for r in LADDER if r[0] in keep]

def timed(cmd, cwd, timeout):
    """Run under /usr/bin/time -v; return (rc, seconds, max_rss_mb, output)."""
    t0 = time.time()
    try:
        p = subprocess.run(["/usr/bin/time", "-v"] + cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out = p.stdout + p.stderr; rc = p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else "TIMEOUT"; rc = -9
    secs = round(time.time() - t0, 1)
    m = re.search(r"Maximum resident set size \(kbytes\): (\d+)", out)
    rss = round(int(m.group(1)) / 1024) if m else None
    return rc, secs, rss, out

results = json.loads(OUT.read_text()) if OUT.exists() else []
def save(): OUT.write_text(json.dumps(results, indent=1))

for tag, procs, policies, models, kinds, groups, max_log, vdepth in LADDER:
    d = generate(tag, procs, policies, models, kinds, groups, max_log)
    print(f"\n=== {tag}: {procs} procs / {policies} policies / {models} models / {kinds} kinds / {groups} groups (MAX_LOG={max_log})", flush=True)
    for spec in ("st.qnt", "groups.qnt"):
        row = dict(tag=tag, spec=spec, procs=procs, policies=policies, models=models, kinds=kinds, groups=groups, max_log=max_log)
        rc, secs, rss, out = timed([QUINT, "typecheck", spec], d, 600)
        row["typecheck_s"] = secs; row["typecheck_ok"] = (rc == 0)
        if rc != 0:
            row["error"] = out[-800:]; print(f"  {spec}: typecheck FAILED in {secs}s"); results.append(row); save(); continue
        def sim(backend):
            rc, secs, rss, out = timed([QUINT, "run", spec, "--invariant=allInvariants", f"--max-samples={SAMPLES}", f"--max-steps={STEPS}", "--seed=7", f"--backend={backend}"], d, 1800)
            crashed = "TypeError" in out or "panicked" in out
            res = "timeout" if rc == -9 else "crash" if crashed else "ok" if "[ok]" in out else "violation" if "[violation]" in out else "error"
            m = re.search(r"\((\d+)ms at (\d+) traces/second\)", out)
            return dict(sim_s=secs, sim_rss_mb=rss, sim_result=res, traces_per_s=int(m.group(2)) if m else None, backend=backend)
        r = sim("rust")
        if r["sim_result"] in ("crash", "error"):
            row["rust_result"] = r["sim_result"]; r = sim("typescript")   # the Rust simulator crashes on large states; fall back
        row.update(r)
        print(f"  {spec}: typecheck {row['typecheck_s']}s · sim {SAMPLES}×{STEPS} [{row['backend']}]: {row['sim_result']} in {row['sim_s']}s, {row['traces_per_s']} traces/s, {row['sim_rss_mb']} MB", flush=True)
        # one witness, to confirm the nondet still explores at this size
        w = "witnessCascade" if spec == "st.qnt" else "witnessClaimWindow"
        rc, secs, rss, out = timed([QUINT, "run", spec, f"--invariant=not({w})", f"--max-samples={SAMPLES * 4}", f"--max-steps={STEPS}", "--seed=5", f"--backend={row['backend']}"], d, 1800)
        row["witness"] = w; row["witness_reached"] = "[violation]" in out; row["witness_s"] = secs
        print(f"    {w}: {'reached' if row['witness_reached'] else 'NOT reached'} in {secs}s", flush=True)
        if vdepth and not NO_VERIFY:
            rc, secs, rss, out = timed([QUINT, "verify", spec, "--invariant=allInvariants", f"--max-steps={vdepth}"], d, 1500)
            row.update(verify_depth=vdepth, verify_s=secs, verify_rss_mb=rss,
                       verify_result=("ok" if "[ok]" in out else "violation" if "[violation]" in out else "timeout" if rc == -9 else "error"))
            print(f"    apalache depth {vdepth}: {row['verify_result']} in {secs}s, {rss} MB", flush=True)
        results.append(row); save()
print("\nwrote", OUT)
