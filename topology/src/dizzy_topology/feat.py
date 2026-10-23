"""The slice of a DIZZY feature-file a topology reads.

DIZZY owns the feat format and validates it (`dizzy generate` refuses an invalid feat).
This package reads only the names and links a placement needs, and ignores every other
key, so the feat format can grow without this package moving in step. `just foogle` and
`just assay` read DIZZY's own feats through it.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class _Element(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class ModelDef(_Element):
    pass


class CommandDef(_Element):
    pass


class QueryDef(_Element):
    model: str | None = None


class ProcedureDef(_Element):
    command: str
    queries: list[str] = []


class PolicyDef(_Element):
    queries: list[str] = []


class ProjectionDef(_Element):
    model: str | None = None


class FeatureDefinition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    models: list[ModelDef] = []
    queries: list[QueryDef] = []
    commands: list[CommandDef] = []
    procedures: list[ProcedureDef] = []
    policies: list[PolicyDef] = []
    projections: list[ProjectionDef] = []


def _named(section: dict | None) -> list[dict]:
    """A feat section is a mapping of name to definition (a bare string is a description)."""
    return [
        {"name": name, **(value if isinstance(value, dict) else {})}
        for name, value in (section or {}).items()
    ]


def load_feat(path: str | Path) -> FeatureDefinition:
    """Read the sections of a ``.feat.yaml`` a topology places or links against."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return FeatureDefinition.model_validate(
        {section: _named(raw.get(section)) for section in FeatureDefinition.model_fields}
    )


def validate_feat(feat: FeatureDefinition) -> list[str]:
    """Check the references a placement follows. The rest is DIZZY's to check."""
    errors: list[str] = []
    commands = {c.name for c in feat.commands}
    queries = {q.name for q in feat.queries}
    models = {m.name for m in feat.models}

    for proc in feat.procedures:
        if proc.command not in commands:
            errors.append(
                f"procedure '{proc.name}': command '{proc.command}' not declared in commands"
            )
    for kind, readers in (("procedure", feat.procedures), ("policy", feat.policies)):
        for reader in readers:
            for q in reader.queries:
                if q not in queries:
                    errors.append(f"{kind} '{reader.name}': query '{q}' not declared in queries")
    for kind, users in (("projection", feat.projections), ("query", feat.queries)):
        for user in users:
            if user.model is not None and user.model not in models:
                errors.append(f"{kind} '{user.name}': model '{user.model}' not declared in models")
    return errors


def pascal_case(name: str) -> str:
    """``book_added`` → ``BookAdded``: the class name DIZZY generates for a feat name."""
    return "".join(part[:1].upper() + part[1:] for part in name.split("_"))
