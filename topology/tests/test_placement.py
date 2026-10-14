import json
import textwrap
from pathlib import Path

from dizzy.feat_schema import FeatureDefinition
from typer.testing import CliRunner

from dizzy_topology.cli import app
from dizzy_topology.placement import FORMAT, resolve_placement
from dizzy_topology.topology_loader import validate_topology
from dizzy_topology.topology_schema import Topology


def _feat() -> FeatureDefinition:
    """Two procedures with different command names, so placement visibly keys on the
    command (what the store records) and not on the procedure."""
    return FeatureDefinition.model_validate(
        {
            "commands": [{"name": "start_batch"}, {"name": "crunch"}],
            "events": [{"name": "batch_started"}, {"name": "crunched"}],
            "procedures": [
                {
                    "name": "open_batch",
                    "description": "d",
                    "command": "start_batch",
                    "emits": ["batch_started"],
                },
                {
                    "name": "crunch_numbers",
                    "description": "d",
                    "command": "crunch",
                    "emits": ["crunched"],
                },
            ],
        }
    )


def _topology(raw: dict) -> Topology:
    topology = Topology.model_validate(raw)
    assert validate_topology(topology, _feat()) == []
    return topology


def test_no_groups_is_one_inline_lane() -> None:
    placement = resolve_placement(Topology(), _feat())
    assert placement == {
        "format": FORMAT,
        "lanes": {"main": {"executor": "inline", "workers": 1}},
        "default_lane": "main",
        "commands": {},
        "waivers": {},
    }


def test_commands_take_their_procedures_lane() -> None:
    topology = _topology(
        {
            "groups": [
                {"name": "main", "catch_all": True},
                {
                    "name": "cpu",
                    "include": [{"kind": "procedures", "names": ["crunch_numbers"]}],
                    "executor": "pool",
                    "workers": 3,
                },
            ],
            "priority": {"interactive": ["start_*"]},
        }
    )
    placement = resolve_placement(topology, _feat())
    assert placement["lanes"] == {
        "main": {"executor": "inline", "workers": 1},
        "cpu": {"executor": "pool", "workers": 3},
    }
    assert placement["commands"] == {
        "crunch": {"lane": "cpu", "priority": "batch"},
        "start_batch": {"lane": "main", "priority": "interactive"},
    }
    assert placement["default_lane"] == "main"


def test_workers_inherit_topology_defaults() -> None:
    topology = _topology(
        {
            "defaults": {"workers": 5},
            "groups": [
                {"name": "main", "catch_all": True},
                {
                    "name": "cpu",
                    "include": [{"kind": "procedures", "names": ["crunch_numbers"]}],
                    "executor": "pool",
                },
            ],
        }
    )
    assert resolve_placement(topology, _feat())["lanes"]["cpu"]["workers"] == 5


def test_default_lane_prefers_inline_and_falls_back_to_first_declared() -> None:
    pools_only = _topology(
        {
            "groups": [
                {"name": "a", "catch_all": True, "executor": "pool"},
            ],
        }
    )
    assert resolve_placement(pools_only, _feat())["default_lane"] == "a"


def test_place_writes_json_and_refuses_an_invalid_topology(tmp_path: Path) -> None:
    feat_file = tmp_path / "f.feat.yaml"
    feat_file.write_text(
        textwrap.dedent(
            """
            description: placement fixture
            commands:
              start_batch: {description: d}
            events:
              batch_started: {description: d}
            procedures:
              open_batch:
                description: d
                command: start_batch
                emits: [batch_started]
            """
        )
    )
    good = tmp_path / "topology.yaml"
    good.write_text("groups:\n  - name: main\n    catch_all: true\n")
    out = tmp_path / "placement.json"
    result = CliRunner().invoke(app, ["place", str(feat_file), str(good), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["commands"] == {
        "start_batch": {"lane": "main", "priority": "batch"}
    }

    # Unclaimed procedure, no catch_all: rule 5. Nothing may be written.
    bad = tmp_path / "bad.yaml"
    bad.write_text("groups:\n  - name: main\n")
    out.unlink()
    result = CliRunner().invoke(app, ["place", str(feat_file), str(bad), "-o", str(out)])
    assert result.exit_code == 1
    assert not out.exists()
