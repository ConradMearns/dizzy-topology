#!/bin/bash
# Retry the Apalache rungs that OOM'd on the default 4 GB heap.
export PATH=$HOME/.bun/bin:$PATH JVM_ARGS="-Xmx24G"
for spec_rung in "st.qnt p10 1" "groups.qnt p25 1" "groups.qnt p50 1" "st.qnt p25 1"; do
  set -- $spec_rung; spec=$1; rung=$2; depth=$3
  start=$(date +%s)
  out=$(cd scale/$rung && timeout 2400 quint verify $spec --invariant=allInvariants --max-steps=$depth 2>&1)
  secs=$(( $(date +%s) - start ))
  verdict=$(echo "$out" | grep -oE '^\[(ok|violation)\]' | head -1); [ -z "$verdict" ] && verdict="error: $(echo "$out" | grep -iE 'error|heap' | tail -1 | cut -c1-120)"
  echo "$rung $spec depth $depth: $verdict (${secs}s, heap 24G)"
done
