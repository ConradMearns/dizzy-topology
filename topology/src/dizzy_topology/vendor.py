"""Install the topology engine into a generated DIZZY Python workspace.

A DIZZY app runs inside the uv workspace ``dizzy generate`` writes at
``<app>/lib/python-uv/``. Its runtime packages are vendored into that workspace as
members: ``generate static`` copies in the kit (``dizzy-kit``), and ``generate wiring``
the Simple Engine (``dizzy-engine``). Neither is on a package index, and neither is the
topology engine. So :func:`vendor` installs the topology engine the same way. It copies
``dizzy_topology_engine`` to ``lib/python-uv/dizzy-topology-engine/`` and adds that
directory to the workspace's members. The next ``uv sync`` installs it beside the
engine it builds on.

Run it after ``generate wiring``, and again after any ``generate libraries``, which
rewrites the members list (dropping ``wiring`` and ``dizzy-engine`` too, which is why
``generate wiring`` must follow it).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import shutil
import tomllib
from pathlib import Path

ENGINE = "dizzy-topology-engine"
"""The engine's distribution name, and the member directory it is vendored as."""

_NEEDS = ("dizzy-kit", "dizzy-engine", "wiring")


class VendorError(Exception):
    pass


def engine_package() -> Path:
    """The installed ``dizzy_topology_engine`` package directory, located without
    importing it (its imports are the app's runtime, not this build tool's)."""
    spec = importlib.util.find_spec("dizzy_topology_engine")
    if spec is None or not spec.submodule_search_locations:
        raise VendorError(
            "dizzy_topology_engine is not installed beside dizzy-topology — run the CLI "
            "from the dizzy-topology workspace (`uv run --project <dizzy-topology> ...`)"
        )
    return Path(next(iter(spec.submodule_search_locations)))


def _pyproject(members: list[str]) -> str:
    """The vendored engine's manifest, from its installed metadata. A requirement the
    workspace has a member for (the kit and the Simple Engine) resolves to that member."""
    dist = importlib.metadata.distribution(ENGINE)
    requires = dist.requires or []
    names = [re.split(r"[\s<>=!~;\[]", r, maxsplit=1)[0] for r in requires]
    sources = "".join(f"{n} = {{ workspace = true }}\n" for n in names if n in members)
    deps = "".join(f'    "{r}",\n' for r in requires)
    return (
        "# Vendored by `dizzy-topology vendor`: the topology engine, installed into this\n"
        "# workspace beside the Simple Engine it builds on. Regenerated, never edited.\n"
        "[project]\n"
        f'name = "{ENGINE}"\n'
        f'version = "{dist.version}"\n'
        f'requires-python = "{dist.metadata["Requires-Python"] or ">=3.11"}"\n'
        f"dependencies = [\n{deps}]\n\n"
        f"[tool.uv.sources]\n{sources}\n"
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/dizzy_topology_engine"]\n'
    )


def _add_member(manifest: Path, text: str) -> None:
    """Append ENGINE to the workspace's ``members`` list, leaving the rest of the file
    as ``dizzy generate`` wrote it."""
    pattern = re.compile(r"(members\s*=\s*\[)(.*?)(\n?\])", re.DOTALL)
    match = pattern.search(text)
    if match is None:
        raise VendorError(f"{manifest}: no [tool.uv.workspace] members list")
    body = match.group(2).rstrip()
    sep = "" if body.endswith(",") or not body.strip() else ","
    updated = text[: match.start(2)] + f'{body}{sep}\n  "{ENGINE}",' + text[match.start(3) :]
    tomllib.loads(updated)  # never write a manifest uv cannot read
    manifest.write_text(updated)


def vendor(app_dir: Path) -> Path:
    """Install the topology engine into *app_dir*'s ``lib/python-uv`` workspace.
    Idempotent: a second run replaces the copy and leaves the members list alone.
    Returns the vendored member's directory."""
    lib = app_dir / "lib" / "python-uv"
    manifest = lib / "pyproject.toml"
    if not manifest.is_file():
        raise VendorError(
            f"{manifest} does not exist — generate the app first "
            "(`dizzy generate static`, `libraries`, then `wiring`)"
        )
    text = manifest.read_text()
    members = tomllib.loads(text).get("tool", {}).get("uv", {}).get("workspace", {})
    members = list(members.get("members", []))
    missing = [m for m in _NEEDS if m not in members]
    if missing:
        raise VendorError(
            f"{manifest} has no {', '.join(missing)} member — run `dizzy generate wiring` "
            "(after `generate libraries`, which drops it), then vendor"
        )

    target = lib / ENGINE
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        engine_package(),
        target / "src" / "dizzy_topology_engine",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (target / "pyproject.toml").write_text(_pyproject(members))
    if ENGINE not in members:
        _add_member(manifest, text)
    return target
