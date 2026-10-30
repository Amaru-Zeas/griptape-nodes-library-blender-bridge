"""Blender Bridge library package.

Importing this package starts the local HTTP bridge server (idempotent) so the
Blender add-on can exchange media and scene commands with GTN.
"""

from __future__ import annotations

try:
    from . import bridge_server as _bridge_server
except ImportError:  # pragma: no cover - fallback when loaded as top-level module
    import bridge_server as _bridge_server  # type: ignore[no-redef]

from .blender_bridge_node import BlenderBridgeNode
from .blender_scene_command_node import BlenderSceneCommandNode

# Best-effort: ensure the bridge server is running as soon as the library loads.
try:
    _bridge_server.ensure_server_started()
except Exception:
    pass

__all__ = ["BlenderBridgeNode", "BlenderSceneCommandNode"]
