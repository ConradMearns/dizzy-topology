foogle_golden := "ext/dizzy/examples/foogle_shindig/conformance/log.jsonl"

# Run the test suite
test:
    uv run pytest

# Re-snapshot intentionally changed output
test-update:
    uv run pytest --snapshot-update

lint:
    uv run ruff check .

# The Simple Engine conformance scenario (DIZZY's foogle_shindig) on the topology engine:
# all-inline must reproduce DIZZY's golden log byte for byte, crash runs included; the
# pooled topology must reproduce its facts and read model.
foogle: foogle-gen
    PYTHONPATH={{justfile_directory()}}/engine/src uv run --project .build/foogle/lib/python-uv python engine/conformance/foogle.py {{foogle_golden}}
    PYTHONPATH={{justfile_directory()}}/engine/src uv run --project .build/foogle/lib/python-uv python engine/conformance/foogle.py {{foogle_golden}} .build/foogle/placement.json

# Generate foogle_shindig from the pinned submodule into .build/, and place it
foogle-gen:
    rm -rf .build/foogle
    mkdir -p .build
    cp -r ext/dizzy/examples/foogle_shindig .build/foogle
    uv run dizzy generate static .build/foogle/foogle_shindig.feat.yaml .build/foogle
    uv run dizzy generate libraries .build/foogle/foogle_shindig.feat.yaml .build/foogle
    uv run dizzy generate wiring .build/foogle/foogle_shindig.feat.yaml .build/foogle
    uv run dizzy-topology place .build/foogle/foogle_shindig.feat.yaml engine/conformance/foogle.topology.yaml -o .build/foogle/placement.json

fmt:
    uv run ruff format .

# The topology engine's Quint spec: simulate every invariant, then prove them to depth 5
spec-engine:
    cd spec/specs && quint run engine.qnt --invariant=allInvariants --max-samples=3000 --max-steps=25 --seed=7
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
    uv run gen-pydantic topology/src/dizzy_topology/def/topology.yaml > topology/src/dizzy_topology/topology_schema.py


# DIZZY submodule (ext/dizzy)

# Fetch/refresh the DIZZY submodule at the pinned revision (run after cloning)
dizzy-init:
    git submodule update --init --recursive ext/dizzy
    just vendor

# Copy DIZZY's Python runtime packages (dizzy-kit, dizzy-engine) out of the pinned
# submodule into vendor/python/, as `dizzy generate wiring` does for an app. Re-run
# after bumping ext/dizzy.
vendor:
    rm -rf vendor/python
    mkdir -p vendor/python
    cp -r ext/dizzy/dizzy/src/dizzy/runtimes/python/dizzy-kit vendor/python/
    cp -r ext/dizzy/dizzy/src/dizzy/runtimes/python/dizzy-engine vendor/python/

# Switch the submodule to a working branch before editing (avoids detached HEAD)
dizzy-dev branch="main":
    cd ext/dizzy && git checkout {{branch}}

# Run seeds against DIZZY's own tracker, e.g. `just dizzy-seeds ready`
dizzy-seeds *args:
    cd ext/dizzy && sd {{args}}

# Push DIZZY changes upstream, then stage the new pointer here to re-pin
dizzy-push:
    cd ext/dizzy && git push
    git add ext/dizzy
    @echo "DIZZY pushed. Submodule pointer staged — commit it here to re-pin."
