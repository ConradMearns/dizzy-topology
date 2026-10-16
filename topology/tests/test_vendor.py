"""`dizzy-topology vendor`: the topology engine installed into a generated app workspace.

`just assay` and `just foogle` run the real thing end to end (generate, vendor, uv sync,
run). These pin the contract on a workspace manifest shaped like `dizzy generate` writes.
"""

import tomllib
from pathlib import Path

import pytest

from dizzy_topology.vendor import ENGINE, VendorError, engine_package, vendor

GENERATED = """\
[tool.uv.workspace]
members = [
  "dizzy-kit",
  "gen_def",
  "gen_int",
  "procedure/parse_run",
  "wiring",
  "dizzy-engine",
]
"""


def _app(tmp_path: Path, manifest: str = GENERATED) -> Path:
    lib = tmp_path / "lib" / "python-uv"
    lib.mkdir(parents=True)
    (lib / "pyproject.toml").write_text(manifest)
    return tmp_path


def _members(app: Path) -> list[str]:
    text = (app / "lib" / "python-uv" / "pyproject.toml").read_text()
    return tomllib.loads(text)["tool"]["uv"]["workspace"]["members"]


def test_vendor_copies_the_engine_and_adds_the_member(tmp_path: Path) -> None:
    app = _app(tmp_path)
    target = vendor(app)
    assert target == app / "lib" / "python-uv" / ENGINE
    assert _members(app) == [
        "dizzy-kit", "gen_def", "gen_int", "procedure/parse_run", "wiring", "dizzy-engine",
        ENGINE,
    ]  # fmt: skip

    source = engine_package()
    copied = target / "src" / "dizzy_topology_engine"
    want = sorted(p.relative_to(source) for p in source.rglob("*.py"))
    assert sorted(p.relative_to(copied) for p in copied.rglob("*.py")) == want
    for rel in want:
        assert (copied / rel).read_text() == (source / rel).read_text()
    assert not list(copied.rglob("__pycache__"))


def test_the_vendored_manifest_resolves_against_the_apps_members(tmp_path: Path) -> None:
    """The engine builds on the kit and the Simple Engine the app already vendors, so
    those resolve to the workspace's own members, never to an index."""
    manifest = tomllib.loads((vendor(_app(tmp_path)) / "pyproject.toml").read_text())
    assert manifest["project"]["name"] == ENGINE
    deps = manifest["project"]["dependencies"]
    assert {"dizzy-kit", "dizzy-engine"} <= set(deps)
    assert manifest["tool"]["uv"]["sources"] == {
        "dizzy-kit": {"workspace": True},
        "dizzy-engine": {"workspace": True},
    }


def test_vendor_is_idempotent(tmp_path: Path) -> None:
    app = _app(tmp_path)
    vendor(app)
    stale = app / "lib" / "python-uv" / ENGINE / "src" / "dizzy_topology_engine" / "old.py"
    stale.write_text("")
    vendor(app)
    assert _members(app).count(ENGINE) == 1
    assert not stale.exists()


def test_vendor_needs_the_wiring_stage(tmp_path: Path) -> None:
    """`generate libraries` rewrites the members and drops wiring and dizzy-engine; the
    engine cannot install against a Simple Engine the workspace does not have."""
    app = _app(tmp_path, GENERATED.replace('  "wiring",\n  "dizzy-engine",\n', ""))
    with pytest.raises(VendorError, match="generate wiring"):
        vendor(app)
    assert not (app / "lib" / "python-uv" / ENGINE).exists()


def test_vendor_needs_a_generated_app(tmp_path: Path) -> None:
    with pytest.raises(VendorError, match="generate the app first"):
        vendor(tmp_path)
