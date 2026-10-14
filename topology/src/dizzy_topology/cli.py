"""``dizzy-topology`` — the build-time verbs: check a topology, resolve it to placement
data for the engine, and profile a captured run.

Ported from the ``engine-topology-v2`` branch of DIZZY, where the check ran inside
``dizzy generate wiring`` and ``profile`` was a ``dizzy`` verb.
"""

import json
from pathlib import Path
from typing import Annotated

import typer
import yaml
from dizzy.feat_loader import load_feat, validate_feat
from dizzy.feat_schema import FeatureDefinition
from dizzy.logger import logger, setup_logging
from pydantic import ValidationError

from dizzy_topology.placement import resolve_placement
from dizzy_topology.profiler import parse_records, render_profile
from dizzy_topology.topology_loader import load_topology, validate_topology
from dizzy_topology.topology_schema import Topology

app = typer.Typer(no_args_is_help=True)


@app.callback()
def _setup() -> None:
    setup_logging()


def _load_valid_feat(feat_file: Path) -> FeatureDefinition:
    # Every input here arrives from outside the build — a hand-typed path, a capture from
    # another machine — so every failure is an expected user mistake and gets the
    # log-then-exit shape, never a traceback.
    try:
        feat = load_feat(feat_file)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        logger.error("could not load feature-file %s: %s", feat_file, exc)
        raise typer.Exit(code=1) from exc

    errors = validate_feat(feat)
    for err in errors:
        logger.error("%s", err)
    if errors:
        raise typer.Exit(code=1)
    return feat


def _load_valid_topology(topology_file: Path, feat: FeatureDefinition) -> Topology:
    try:
        topology = load_topology(topology_file)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        # topology.yaml is hand-authored, so a typo'd key is the expected failure.
        logger.error("%s is not valid: %s", topology_file, exc)
        raise typer.Exit(code=1) from exc

    topology_errors = validate_topology(topology, feat)
    for err in topology_errors:
        logger.error("%s", err)
    if topology_errors:
        raise typer.Exit(code=1)
    return topology


@app.command()
def check(
    feat_file: Annotated[Path, typer.Argument(help="Path to the .feat.yaml file")],
    topology_file: Annotated[Path, typer.Argument(help="Path to the topology.yaml file")],
) -> None:
    """Validate a topology.yaml against the feature-file it places."""
    topology = _load_valid_topology(topology_file, _load_valid_feat(feat_file))
    # Say so: without a positive signal, "my topology is enforced" and "my topology is
    # ignored" look identical.
    logger.info("Validated %s — %d consumer groups", topology_file, len(topology.groups or []))


@app.command()
def place(
    feat_file: Annotated[Path, typer.Argument(help="Path to the .feat.yaml file")],
    topology_file: Annotated[Path, typer.Argument(help="Path to the topology.yaml file")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the placement here instead of stdout"),
    ] = None,
) -> None:
    """Validate a topology.yaml, then resolve it to the placement JSON the topology
    engine reads at run time."""
    feat = _load_valid_feat(feat_file)
    placement = resolve_placement(_load_valid_topology(topology_file, feat), feat)
    text = json.dumps(placement, indent=2) + "\n"
    if output is None:
        typer.echo(text, nl=False)
    else:
        output.write_text(text)
        logger.info("Wrote %s — %d lanes", output, len(placement["lanes"]))


@app.command()
def profile(
    feat_file: Annotated[Path, typer.Argument(help="Path to the .feat.yaml file")],
    capture: Annotated[
        Path,
        typer.Argument(help="JSONL bus capture written by a JsonlBusSink during the run"),
    ],
) -> None:
    """Stage profile of a captured run: where the wall clock went per element, policy
    fan-out, and whether the workload is serial-bound or pool-eligible."""
    feat = _load_valid_feat(feat_file)
    try:
        # errors="replace": one binary-garbage byte (a crashed writer, shell noise) must
        # cost that line — parse_records skips and counts it — never the whole capture.
        lines = capture.read_text(errors="replace").splitlines()
    except OSError as exc:
        logger.error("could not read capture %s: %s", capture, exc)
        raise typer.Exit(code=1) from exc

    records, skipped = parse_records(lines)
    if skipped:
        # Skipping is tolerable (a truncated final line is normal telemetry loss), but it
        # must never be silent: a half-read capture is a half-true profile.
        logger.warning("skipped %d unparseable line(s) in %s", skipped, capture)
    if not records:
        logger.error("no parseable records in %s — is it a JsonlBusSink capture?", capture)
        raise typer.Exit(code=1)

    typer.echo(render_profile(feat, records))


def main() -> None:
    app()
