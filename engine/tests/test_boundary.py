"""The engine is a runtime dependency of apps; DIZZY is a build dependency. Enforced
here rather than trusted: an app that runs this engine has dizzy_kit and dizzy_engine
vendored beside it, and nothing else of DIZZY."""

import ast
from pathlib import Path

import dizzy_topology_engine

ALLOWED_DIZZY_ROOTS = {"dizzy_kit", "dizzy_engine", "dizzy_topology_engine"}


def test_the_engine_imports_nothing_from_the_build_side() -> None:
    package = Path(dizzy_topology_engine.__file__).parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module.split(".")[0]]
            else:
                continue
            for root in roots:
                if root.startswith("dizzy") and root not in ALLOWED_DIZZY_ROOTS:
                    offenders.append(f"{path.name}:{node.lineno} imports {root}")
    assert not offenders, offenders
