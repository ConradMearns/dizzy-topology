#!/usr/bin/env python3
"""Render report/results.json (+ optional review_*.json) to report/index.html."""
import html, json, sys
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parent
data = json.loads((OUT / "results.json").read_text())
meta, checks = data["meta"], data["checks"]

def esc(s): return html.escape(str(s), quote=True)

reviews = {}
for name in ("st", "groups"):
    p = OUT / f"review_{name}.json"
    if p.exists():
        reviews[name] = json.loads(p.read_text())

SPEC_INTRO = {
  "engine.qnt": ("Spec 4 — the topology engine (as shipped)",
    "The Simple Engine with lanes: an inline lane on the host thread and a process-pool lane, over the Simple Engine "
    "store. Every transaction boundary is its own step — claim, append+ack, fold+enqueue, cursor — and a crash may fall "
    "between any two, losing the read model and in-flight pool work but never the store. Graph and rule-3 placement are "
    "drawn at <code>init</code>. Headline: exactly-once effects hold <em>unconditionally</em> (st needed an application "
    "guard), and reads stay fresh with pools although the log leaves claim order. The Python engine is held to the same "
    "invariants by <code>engine/tests/test_spec_conformance.py</code>."),
  "st.qnt": ("Spec 1 — single-threaded engine",
    "One hybrid group, one lane, one cursor. <code>run_command</code> is a single atomic step: "
    "claim → run procedure → append → fold → commit → run policies → dispatch. The feature graph is "
    "drawn nondeterministically at <code>init</code>, so every check ranges over every feature of the bounded size "
    "(2 procedures, 1 policy, 2 models, 2 event kinds ≈ 10⁶ graphs)."),
  "groups.qnt": ("Spec 2 — consumer groups (distributed)",
    "The same graph split across two groups, each with its own cursor over the shared log and its own command lane; "
    "the read substrate is shared but each model is folded only by its owner. Placement is drawn nondeterministically "
    "with the graph. The claim under test is <code>rule6 ⇒ freshReads ∧ freshPolicyReads</code>: a reader that shares a "
    "group with every model it reads observes exactly what the single-threaded engine would."),
}

def badge(c):
    if c["passed"]:
        return f'<span class="pill pass">{"holds" if c["kind"]!="witness" else "reachable"}</span>'
    return f'<span class="pill fail">{esc(c["result"])}</span>'

def kind_label(k):
    return {"invariant": "invariant · simulation", "witness": "witness · reachability", "verify": "Apalache · bounded"}[k]

def table(spec):
    rows = [c for c in checks if c["spec"] == spec]
    out = []
    for c in rows:
        trace = ""
        if c.get("trace"):
            trace = f'<details><summary>counterexample</summary><pre>{esc(c["trace"])}</pre></details>'
        out.append(f"""<tr>
<td><code>{esc(c["target"])}</code>{trace}</td>
<td class="kind">{kind_label(c["kind"])}</td>
<td>{esc(c["note"])}</td>
<td class="num">{c["seconds"]}s</td>
<td>{badge(c)}</td></tr>""")
    return "\n".join(out)

def review_block(name):
    r = reviews.get(name)
    if not r: return ""
    def items(lst, cls):
        if not lst: return "<p class='muted'>none</p>"
        o = []
        for x in lst:
            f = x["finding"]
            votes = "".join(f"<li><strong>{'refuted' if v['refuted'] else 'stands'}</strong> — {esc(v['reasoning'])}"
                            + (f"<br><em>fix:</em> {esc(v['corrected_fix'])}" if v.get('corrected_fix') and not v['refuted'] else "")
                            + "</li>" for v in x.get("votes", []))
            if x.get("disposition"):
                votes = f"<li><strong>{esc(x['disposition'])}</strong> — {esc(x['note'])}</li>"
            o.append(f"""<details class="finding {cls}"><summary><span class="sev">{esc(f['severity'])}</span> {esc(f['title'])}
<span class="lens">{esc(x.get('lens', ''))}</span></summary>
<p>{esc(f['claim'])}</p><p class="ref"><code>{esc(f['spec_ref'])}</code> · <code>{esc(f['code_ref'])}</code></p>
<p><em>suggested fix:</em> {esc(f['suggested_fix'])}</p><ul class="votes">{votes}</ul></details>""")
        return "\n".join(o)
    return f"""<section class="review">
<h3>Adversarial review</h3>

<h4>Accepted — fixed in this revision ({len(r['confirmed'])})</h4>{items(r['confirmed'], 'confirmed')}
<h4>Deferred or rejected ({len(r['contested'])})</h4>{items(r['contested'], 'contested')}
{('<h4>Disposition</h4>' + r['disposition']) if r.get('disposition') else ''}
</section>"""

def source(spec):
    return f'<details class="src"><summary>source · <code>specs/{esc(spec)}</code></summary><pre>{esc((ROOT/"specs"/spec).read_text())}</pre></details>'

# ---- scale ladder (optional) ----
def scale_section():
    p = ROOT / "scale" / "results.json"
    if not p.exists(): return ""
    rows = json.loads(p.read_text())
    if not rows: return ""
    def cell(v, suf=""): return "—" if v is None else f"{v}{suf}"
    def res(r):
        if r.get("error"): return '<span class="pill fail">typecheck failed</span>'
        cls = "pass" if r.get("sim_result") == "ok" else "fail"
        return f'<span class="pill {cls}">{esc(r.get("sim_result"))}</span>'
    def ver(r):
        if "verify_result" not in r: return '<span class="muted">not attempted</span>'
        cls = "pass" if r["verify_result"] == "ok" else "fail"
        heap = f' · heap {r["verify_heap"]}' if r.get("verify_heap") else ""
        return f'<span class="pill {cls}">depth {r["verify_depth"]} · {esc(r["verify_result"])}</span> <span class="muted">{cell(r.get("verify_s"),"s")} · {cell(r.get("verify_rss_mb"),"MB")}{heap}</span>'
    trs = "".join(f"""<tr><td class="mono">{esc(r['tag'])}</td><td class="num">{r['procs']} / {r['policies']} / {r['models']} / {r['kinds']} / {r['groups']}</td>
<td><code>{esc(r['spec'])}</code></td><td class="num">{cell(r.get('typecheck_s'),'s')}</td><td class="num">{cell(r.get('traces_per_s'))}</td>
<td class="num">{cell(r.get('sim_s'),'s')}</td><td class="num">{cell(r.get('sim_rss_mb'),' MB')}</td><td>{res(r)}</td>
<td>{'reached' if r.get('witness_reached') else '<span class="muted">not in budget</span>'}</td><td>{ver(r)}</td></tr>""" for r in rows)
    return f"""<section><h2>Scale ladder <span class="count">{len(set(r['tag'] for r in rows))} rungs</span></h2>
<p class="intro">The same two engine specs, unchanged, over generated universes of increasing size (<code>scale/gen.py</code>, <code>scale/bench.py</code>).
Simulation is 300 traces × 20 steps; the witness column runs 1200 traces looking for one reachable trace of <code>witnessCascade</code> (st) or <code>witnessClaimWindow</code> (groups).
Peak memory is the process's maximum resident set.</p>
<div class="tbl"><table><thead><tr><th>rung</th><th class="num">procs / policies / models / kinds / groups</th><th>spec</th><th class="num">typecheck</th><th class="num">traces/s</th><th class="num">sim wall</th><th class="num">peak RSS</th><th>result</th><th>witness</th><th>Apalache</th></tr></thead>
<tbody>{trs}</tbody></table></div></section>"""

total = len(checks); passed = sum(c["passed"] for c in checks)
sections = []
for spec, (title, intro) in SPEC_INTRO.items():
    n = [c for c in checks if c["spec"] == spec]
    sections.append(f"""<section>
<h2>{esc(title)} <span class="count">{sum(c['passed'] for c in n)}/{len(n)}</span></h2>
<p class="intro">{intro}</p>
<div class="tbl"><table><thead><tr><th>check</th><th>method</th><th>what it says</th><th class="num">time</th><th>result</th></tr></thead>
<tbody>{table(spec)}</tbody></table></div>
{review_block(spec.split('.')[0])}
{source(spec)}
</section>""")

page = f"""<title>Dizzy Engine Ledger</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,500;8..60,600&family=IBM+Plex+Sans:wght@400;500&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root{{--bg:#F5F7F4;--ink:#1B1F23;--mute:#5F6B74;--rule:#D6DCD8;--panel:#FFFFFF;--accent:#2F5D8A;--accent-ink:#1F4266;
--pass:#2E7D4F;--pass-bg:#E4F2E9;--fail:#B3261E;--fail-bg:#F9E3E1;--code-bg:#EEF1EE;}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#151A1E;--ink:#E6EAE7;--mute:#98A3AB;--rule:#2C353C;--panel:#1C2328;--accent:#7FB0E0;--accent-ink:#A9CBEE;
--pass:#7ED3A0;--pass-bg:#1C3527;--fail:#F19A93;--fail-bg:#43201D;--code-bg:#232B31;}}}}
:root[data-theme="dark"]{{--bg:#151A1E;--ink:#E6EAE7;--mute:#98A3AB;--rule:#2C353C;--panel:#1C2328;--accent:#7FB0E0;--accent-ink:#A9CBEE;
--pass:#7ED3A0;--pass-bg:#1C3527;--fail:#F19A93;--fail-bg:#43201D;--code-bg:#232B31;}}
body{{background:var(--bg);color:var(--ink);font:16px/1.55 "IBM Plex Sans",system-ui,sans-serif;margin:0;padding:2.5rem 1.25rem 5rem}}
main{{max-width:72rem;margin:0 auto}}
h1,h2,h3{{font-family:"Source Serif 4",Georgia,serif;font-weight:600;text-wrap:balance;letter-spacing:-.01em}}
h1{{font-size:2.4rem;line-height:1.1;margin:0 0 .5rem}} h2{{font-size:1.6rem;margin:3rem 0 .5rem;padding-top:1.5rem;border-top:1px solid var(--rule)}}
h3{{font-size:1.15rem;margin:2rem 0 .25rem}} h4{{font:500 .8rem/1 "IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--mute);margin:1.5rem 0 .5rem}}
code,pre{{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.88em}}
code{{background:var(--code-bg);padding:.1em .35em;border-radius:3px}}
pre{{background:var(--code-bg);padding:1rem;overflow-x:auto;line-height:1.4;font-size:.8rem;margin:.5rem 0 0}}
.lede{{max-width:65ch;color:var(--mute);margin:0 0 1.5rem}} .intro{{max-width:70ch}}
.strip{{display:flex;gap:2.5rem;flex-wrap:wrap;padding:1rem 0;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);margin:1.5rem 0}}
.strip div{{display:flex;flex-direction:column}} .strip b{{font:600 1.7rem/1.1 "Source Serif 4",serif;font-variant-numeric:tabular-nums}}
.strip span{{font:500 .72rem/1.4 "IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--mute)}}
.count{{font:500 .9rem "IBM Plex Mono",monospace;color:var(--mute);margin-left:.5rem}}
.tbl{{overflow-x:auto;background:var(--panel);border:1px solid var(--rule);border-radius:4px}}
table{{border-collapse:collapse;width:100%;font-size:.92rem}} th{{text-align:left;font:500 .72rem/1.4 "IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--mute);padding:.7rem .9rem;border-bottom:1px solid var(--rule)}}
td{{padding:.6rem .9rem;border-bottom:1px solid var(--rule);vertical-align:top}} tr:last-child td{{border-bottom:0}}
td.kind{{color:var(--mute);white-space:nowrap;font-size:.85rem}} .num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.pill{{display:inline-block;font:500 .72rem/1 "IBM Plex Mono",monospace;letter-spacing:.04em;padding:.35em .6em;border-radius:999px;white-space:nowrap}}
.pill.pass{{color:var(--pass);background:var(--pass-bg)}} .pill.fail{{color:var(--fail);background:var(--fail-bg)}}
details{{margin:.4rem 0}} summary{{cursor:pointer;color:var(--accent-ink)}} summary:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
details.src{{margin-top:1.5rem}} details.src pre{{max-height:32rem}}
.finding{{background:var(--panel);border:1px solid var(--rule);border-left:3px solid var(--accent);padding:.5rem .9rem;border-radius:3px}}
.finding.contested{{border-left-color:var(--mute)}} .finding p{{max-width:75ch;margin:.5rem 0}}
.sev{{font:500 .7rem "IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--fail);margin-right:.5rem}}
.lens{{font:.75rem "IBM Plex Mono",monospace;color:var(--mute);margin-left:.5rem}} .ref{{color:var(--mute);font-size:.85rem}}
.votes{{font-size:.88rem;padding-left:1.2rem}} .muted{{color:var(--mute)}}
.mono{{font-family:"IBM Plex Mono",monospace;font-size:.85rem}}
.method{{max-width:70ch}} .method dt{{font-weight:500;margin-top:.6rem}} .method dd{{margin:0;color:var(--mute)}}
</style>
<main>
<h1>Dizzy Engine Ledger</h1>
<p class="lede">Every check run against the Quint engine specifications: what was asked, how it was checked, and what came back.
Generated {esc(meta['generated'])} · quint {esc(meta['quint'])} · dizzy @ <code>{esc(meta['dizzy'])}</code> · {esc(meta['samples'])} samples × 20 steps per simulation.</p>
<div class="strip"><div><b>{passed}/{total}</b><span>checks as expected</span></div>
<div><b>{sum(1 for c in checks if c['kind']=='invariant' and c['passed'])}</b><span>invariants holding</span></div>
<div><b>{sum(1 for c in checks if c['kind']=='witness' and c['passed'])}</b><span>witnesses reachable</span></div>
<div><b>{sum(1 for c in checks if c['kind']=='verify' and c['passed'])}/{sum(1 for c in checks if c['kind']=='verify')}</b><span>bounded proofs</span></div></div>
<dl class="method">
<dt>invariant · simulation</dt><dd>Random traces from the nondeterministic step relation; a violation prints a concrete counterexample.</dd>
<dt>witness · reachability</dt><dd>Runs <code>--invariant=not(X)</code>. A violation is the desired outcome: it is a trace that reaches X, proving the invariants are not vacuous.</dd>
<dt>Apalache · bounded</dt><dd>Symbolic model checking of every behaviour up to the stated depth, across every graph (and placement) the <code>init</code> can draw.</dd>
</dl>
{''.join(sections)}
{scale_section()}
</main>
"""
(OUT / "index.html").write_text(page)
print("wrote report/index.html")
