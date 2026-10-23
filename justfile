foogle_golden := ".dizzy/examples/foogle_shindig/conformance/log.jsonl"

# Run the test suite
test: dizzy
    uv run pytest

# Re-snapshot intentionally changed output
test-update:
    uv run pytest --snapshot-update

lint: dizzy
    uv run ruff check .

# The Simple Engine conformance scenario (DIZZY's foogle_shindig) on the topology engine:
# all-inline must reproduce DIZZY's golden log byte for byte, crash runs included; the
# pooled topology must reproduce its facts and read model.
foogle: foogle-gen
    uv run --project .build/foogle/lib/python-uv python engine/conformance/foogle.py {{foogle_golden}}
    uv run --project .build/foogle/lib/python-uv python engine/conformance/foogle.py {{foogle_golden}} .build/foogle/placement.json

# Generate foogle_shindig from the pinned DIZZY into .build/, install the engine into it
# (as an app would), and place it. `dizzy generate` runs in DIZZY's own locked env.
foogle-gen: dizzy
    rm -rf .build/foogle
    mkdir -p .build
    cp -r .dizzy/examples/foogle_shindig .build/foogle
    uv run --project .dizzy dizzy generate static .build/foogle/foogle_shindig.feat.yaml .build/foogle
    uv run --project .dizzy dizzy generate libraries .build/foogle/foogle_shindig.feat.yaml .build/foogle
    uv run --project .dizzy dizzy generate wiring .build/foogle/foogle_shindig.feat.yaml .build/foogle
    uv run dizzy-topology vendor .build/foogle
    uv run dizzy-topology place .build/foogle/foogle_shindig.feat.yaml engine/conformance/foogle.topology.yaml -o .build/foogle/placement.json

# The worked example: a DIZZY Python app (examples/assay) generated from scratch, the
# topology engine installed into it with `dizzy-topology vendor`, then run all inline and
# under its topology. Both runs must reach the same summary.
assay: dizzy
    rm -rf .build/assay
    mkdir -p .build
    cp -r examples/assay .build/assay
    uv run --project .dizzy dizzy generate static .build/assay/assay.feat.yaml .build/assay
    uv run --project .dizzy dizzy generate libraries .build/assay/assay.feat.yaml .build/assay
    uv run --project .dizzy dizzy generate wiring .build/assay/assay.feat.yaml .build/assay
    uv run dizzy-topology vendor .build/assay
    uv run dizzy-topology place .build/assay/assay.feat.yaml .build/assay/topology.yaml -o .build/assay/placement.json
    uv run --project .build/assay/lib/python-uv python .build/assay/run.py
    uv run --project .build/assay/lib/python-uv python .build/assay/run.py --placement .build/assay/placement.json

fmt:
    uv run ruff format .

# Everything "the engine is correct" rests on, in one command (~5 min): the spec proved,
# its witnesses reachable, the engine held to it (state sweep and step-by-step replay),
# DIZZY's conformance scenario, and the worked example installed into a generated app.
verify: lint test spec-engine spec-witnesses foogle assay

# Every engine.qnt witness must be reachable — an invariant that holds over nothing proves
# nothing. Quint finding a violation of not(w) is a trace that reaches w.
spec-witnesses:
    #!/usr/bin/env bash
    set -euo pipefail
    cd spec/specs
    for w in $(grep -oE "val witness[A-Za-z]+" engine.qnt | cut -d' ' -f2); do
      # The reopen witnesses need a setup too precise for uniform sampling: the scenarios
      # below reach them, and so does biased sampling (engineReopen, after the loop).
      case "$w" in witnessReorderedRead|witnessOrderFreeReordered) continue ;; esac
      out=$(quint run engine.qnt --invariant="not($w)" --max-samples=8000 --max-steps=25 --seed=7 2>&1 || true)
      if grep -q '^\[violation\]' <<< "$out"; then
        echo "reachable    $w"
      else
        echo "UNREACHABLE  $w"; exit 1
      fi
    done
    out=$(quint run engine.qnt --main=engineReopen --init=initCrossStore --step=reopenStep --invariant="not(witnessReorderedRead)" --max-samples=8000 --max-steps=20 --seed=7 2>&1 || true)
    grep -q '^\[violation\]' <<< "$out" && echo "reachable    witnessReorderedRead (engineReopen)" || { echo "UNREACHABLE  witnessReorderedRead"; exit 1; }
    # ...and as scripted `run`s, step by step.
    quint test engine.qnt --main=engine --match='Test$' 2>&1 | grep -E "ok |passing|failing|error"
    quint test engine.qnt --main=engine --match='Test$' > /dev/null
# The topology engine's Quint spec: simulate every invariant, then prove them to depth 5
spec-engine:
    cd spec/specs && quint run engine.qnt --invariant=allInvariants --max-samples=3000 --max-steps=25 --seed=7
    cd spec/specs && quint run engine.qnt --main=engineReopen --init=initCrossStore --step=reopenStep --invariant=allInvariants --max-samples=3000 --max-steps=20 --seed=7
    cd spec/specs && quint verify engine.qnt --invariant=allInvariants --max-steps=5

# Every spec, every invariant and witness — simulation only, few samples
spec:
    cd spec && python3 report/run_checks.py --quick --no-verify

# Every spec at full samples plus every Apalache proof, then the HTML report (~15 min)
spec-full:
    cd spec && python3 report/run_checks.py
    cd spec && python3 report/render.py

# Regenerate the pydantic topology schema from its LinkML source
gen-topology-pydantic:
    uvx --from 'linkml==1.11.1' gen-pydantic topology/src/dizzy_topology/def/topology.yaml > topology/src/dizzy_topology/topology_schema.py


# DIZZY, for development and tests only: the engine's tests need DIZZY's run-time
# packages, and `just foogle` / `just assay` need its `dizzy generate`. Neither package
# depends on DIZZY. Bump `dizzy_rev` to re-pin. DIZZY_REPO points the fetch elsewhere,
# e.g. a local checkout while changing DIZZY alongside this repo.

dizzy_rev := "1527342d14a64f0677a644a4c9b23e139ac4e83e"
dizzy_repo := env("DIZZY_REPO", "https://github.com/PNNL/dizzy")

# Fetch DIZZY at the pin into .dizzy/ and copy its Python runtime packages (dizzy-kit,
# dizzy-engine) into vendor/python/, as `dizzy generate wiring` does for an app. Run after
# cloning and after bumping `dizzy_rev`; a no-op when both are already at the pin.
dizzy:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "$(git -C .dizzy rev-parse HEAD 2>/dev/null)" != "{{dizzy_rev}}" ]; then
      rm -rf .dizzy
      git init -q .dizzy
      git -C .dizzy fetch -q --depth 1 "{{dizzy_repo}}" "{{dizzy_rev}}"
      git -C .dizzy -c advice.detachedHead=false checkout -q FETCH_HEAD
    fi
    if [ "$(cat vendor/.dizzy_rev 2>/dev/null)" != "{{dizzy_rev}}" ]; then
      rm -rf vendor/python
      mkdir -p vendor/python
      cp -r .dizzy/dizzy/src/dizzy/runtimes/python/dizzy-kit vendor/python/
      cp -r .dizzy/dizzy/src/dizzy/runtimes/python/dizzy-engine vendor/python/
      echo "{{dizzy_rev}}" > vendor/.dizzy_rev
    fi
