#!/usr/bin/env python3
"""Generate a sized copy of the specs: scale/<tag>/{graph,st,groups}.qnt.

Only the universe block of graph.qnt changes; st.qnt / groups.qnt are copied verbatim,
so every size checks the same engine semantics.
"""
import re, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "specs"

def universe(procs, policies, models, kinds, groups):
    P = [f"p{i}" for i in range(1, procs + 1)]
    Y = [f"y{i}" for i in range(1, policies + 1)]
    M = [f"m{i}" for i in range(1, models + 1)]
    K = [f"e{i}" for i in range(1, kinds + 1)]
    G = [f"g{i}" for i in range(1, groups + 1)]
    q = lambda xs: ", ".join(f'"{x}"' for x in xs)
    return f'''  // @universe-begin  (generated: {procs} procs, {policies} policies, {models} models, {kinds} kinds, {groups} groups)
  pure val PROCS    = Set({q(P)})
  pure val POLICIES = Set({q(Y)})
  pure val MODELS   = Set({q(M)})
  pure val KINDS    = Set({q(K)})
  pure val GROUPS   = Set({q(G)})
  pure val PROC_LIST   = [{q(P)}]
  pure val POLICY_LIST = [{q(Y)}]
  pure val KIND_LIST   = [{q(K)}]
  // @universe-end
'''

def generate(tag, procs, policies, models, kinds, groups, max_log=None):
    out = ROOT / "scale" / tag
    out.mkdir(parents=True, exist_ok=True)
    g = (SPECS / "graph.qnt").read_text()
    g = re.sub(r"  // @universe-begin.*?// @universe-end\n", universe(procs, policies, models, kinds, groups), g, flags=re.S)
    (out / "graph.qnt").write_text(g)
    for f in ("st.qnt", "groups.qnt"):
        t = (SPECS / f).read_text()
        if max_log: t = t.replace("pure val MAX_LOG = 6", f"pure val MAX_LOG = {max_log}")
        (out / f).write_text(t)
    return out

if __name__ == "__main__":
    tag, *nums = sys.argv[1:]
    print(generate(tag, *map(int, nums)))
