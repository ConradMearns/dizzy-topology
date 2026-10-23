"""The build tooling reads feature-files itself (`feat.py`) and depends on no DIZZY
package. Enforced here rather than trusted, as the engine's boundary is."""

import ast
from pathlib import Path

import dizzy_topology

ALLOWED_DIZZY_ROOTS = {"dizzy_topology", "dizzy_topology_engine"}


def test_the_build_tooling_imports_nothing_from_dizzy() -> None:
    package = Path(dizzy_topology.__file__).parent
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
