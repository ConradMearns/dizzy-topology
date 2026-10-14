"""The topology engine — the Simple Engine with lanes and process pools.

Runs the kit's ``Domain`` tables a generated wiring builds, over the Simple Engine store
format, placing each command on the lane a placement document names
(``dizzy-topology place``). It depends on what ``dizzy generate`` vendors into every app
(``dizzy_kit``, ``dizzy_engine``) and never on DIZZY itself.
"""

from dizzy_topology_engine.engine import TopologyEngine
from dizzy_topology_engine.placement import Lane, Placement, PlacementError

__all__ = ["Lane", "Placement", "PlacementError", "TopologyEngine"]
