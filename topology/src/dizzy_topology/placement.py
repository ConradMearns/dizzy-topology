"""Resolve a validated topology to placement DATA — what crosses from build to run time.

The engine must never read a feature-file or a topology.yaml: doing so would need DIZZY
(to load the feat) and this validator (to trust the topology) at run time. So the build
resolves the two into a plain JSON document, and the engine reads only that.

Placement is generated data and never a hand-written callable: a callable is a second
place the deployment is described, and it drifts from the topology silently.

Keyed on the command's FEAT NAME, because that is what the Simple Engine store records
in ``commands.type`` and what the kit's ``Domain`` dispatches by. (On
``engine-topology-v2`` it was the command class name, read off ``type(command)``.) A
command's lane is its procedure's group: a command is not in the log, so the only thing
that gives it a home is who runs it.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from dizzy.feat_schema import FeatureDefinition

from dizzy_topology.topology_loader import _inherited, group_of
from dizzy_topology.topology_schema import Topology

FORMAT = 1
"""Bumped on any change to the document's shape; the engine refuses a format it does
not know rather than guess at one."""


def resolve_placement(topology: Topology, feat: FeatureDefinition) -> dict[str, Any]:
    """The placement document for a topology ``validate_topology`` has already accepted.

    Shape::

        {"format": 1,
         "lanes": {<group>: {"executor": "inline" | "pool", "workers": <int>}},
         "default_lane": <group>,
         "commands": {<command>: {"lane": <group>, "priority": "interactive" | "batch"}},
         "waivers": {<group>: <who asserted reader isolation>}}

    ``default_lane`` is where a command this document does not name goes: the first
    declared lane that is not a pool, or the first declared lane where a topology
    declares only pools. It is a declared group rather than the string "default", which
    a topology need not have named.
    """
    groups = topology.groups or []
    if not groups:
        # The default topology: one inline lane, every command batch. Spelled out rather
        # than left empty so the engine has one shape to read.
        return {
            "format": FORMAT,
            "lanes": {"main": {"executor": "inline", "workers": 1}},
            "default_lane": "main",
            "commands": {},
            "waivers": {},
        }

    claims = group_of(topology, feat)
    interactive = list((topology.priority.interactive if topology.priority else None) or [])
    defaults = topology.defaults

    commands: dict[str, dict[str, str]] = {}
    for proc in feat.procedures or []:
        lane = claims.get(("procedure", proc.name))
        if lane is None:
            # Unclaimed or doubly claimed: a validated topology contains neither, so skip
            # rather than guess.
            continue
        priority = (
            "interactive"
            if any(fnmatchcase(proc.command, pattern) for pattern in interactive)
            else "batch"
        )
        commands.setdefault(proc.command, {"lane": lane, "priority": priority})

    lanes = {
        g.name: {
            "executor": g.executor or "inline",  # use_enum_values: already the string
            "workers": _inherited(g.workers, defaults.workers if defaults else None, "workers")[0],
        }
        for g in groups
    }
    default_lane = next((g.name for g in groups if lanes[g.name]["executor"] != "pool"), None)

    return {
        "format": FORMAT,
        "lanes": lanes,
        "default_lane": default_lane or groups[0].name,
        "commands": dict(sorted(commands.items())),
        "waivers": {
            g.name: g.reader_isolation_asserted_by for g in groups if g.reader_isolation_asserted_by
        },
    }
