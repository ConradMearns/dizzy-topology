"""The placement document — the only description of the deployment the engine reads.

``dizzy-topology place`` writes it at build time, after validating the topology against
the feature-file. The engine never reads a feat or a topology.yaml: that would need DIZZY
and the validator at run time. It trusts the document's rules (no projection off the
main thread, no reader in a pool) because the build checked them, and checks only what it
can see: that the shape is one it knows and that every name points at a declared lane.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

FORMAT = 1

Executor = Literal["inline", "pool"]
Priority = Literal["interactive", "batch"]


class PlacementError(ValueError):
    """The placement document is not one this engine can run."""


@dataclass(frozen=True)
class Lane:
    name: str
    executor: Executor
    workers: int


@dataclass(frozen=True)
class Placement:
    lanes: Mapping[str, Lane]
    default_lane: str
    commands: Mapping[str, tuple[str, Priority]]
    """Command feat name -> (lane, priority)."""
    waivers: Mapping[str, str]

    @classmethod
    def inline(cls) -> Placement:
        """The default topology: one inline lane, every command batch — the Simple
        Engine's own behaviour."""
        return cls.parse({"format": FORMAT, "lanes": {"main": {"executor": "inline"}}})

    @classmethod
    def load(cls, path: str | Path) -> Placement:
        return cls.parse(json.loads(Path(path).read_text()))

    @classmethod
    def parse(cls, doc: Mapping[str, Any]) -> Placement:
        if doc.get("format") != FORMAT:
            raise PlacementError(
                f"placement format {doc.get('format')!r} is not {FORMAT} — "
                "regenerate it with the dizzy-topology that matches this engine"
            )
        lanes: dict[str, Lane] = {}
        for name, raw in (doc.get("lanes") or {}).items():
            executor = raw.get("executor", "inline")
            workers = raw.get("workers", 1)
            if executor not in ("inline", "pool"):
                raise PlacementError(f"lane {name!r}: unknown executor {executor!r}")
            if not isinstance(workers, int) or workers < 1:
                raise PlacementError(f"lane {name!r}: workers must be a positive integer")
            lanes[name] = Lane(name, executor, workers)
        if not lanes:
            raise PlacementError("placement declares no lanes")

        default_lane = doc.get("default_lane") or next(iter(lanes))
        if default_lane not in lanes:
            raise PlacementError(f"default_lane {default_lane!r} is not a declared lane")

        commands: dict[str, tuple[str, Priority]] = {}
        for command, raw in (doc.get("commands") or {}).items():
            lane, priority = raw.get("lane"), raw.get("priority", "batch")
            if lane not in lanes:
                raise PlacementError(f"command {command!r}: lane {lane!r} is not declared")
            if priority not in ("interactive", "batch"):
                raise PlacementError(f"command {command!r}: unknown priority {priority!r}")
            commands[command] = (lane, priority)

        return cls(lanes, default_lane, commands, dict(doc.get("waivers") or {}))

    def lane_of(self, command: str) -> Lane:
        return self.lanes[self.commands.get(command, (self.default_lane, "batch"))[0]]

    def priority_of(self, command: str) -> Priority:
        return self.commands.get(command, (self.default_lane, "batch"))[1]
