"""MCP server for the Blender <-> Griptape bridge.

Exposes the bridge's scene-command protocol as MCP tools, so any MCP client
(Claude Desktop, Cursor, etc.) can build and inspect the open Blender scene
by text: "make a night rooftop blockout with a wide-angle camera".

Requirements:
* The GTN engine running with the Blender Bridge library (bridge on :7890).
* Blender open with the Griptape Bridge add-on and 'Listen for Commands' ON.
* For the run_python tool: 'Allow Python Commands' enabled in the add-on prefs.

Run (needs the `mcp` package, v1 API — pin below matters):
    uv run --with "mcp<2" python blender_bridge_mcp.py

Client config example (Cursor ~/.cursor/mcp.json or Claude Desktop):
    {
      "mcpServers": {
        "blender": {
          "command": "uv",
          "args": ["run", "--with", "mcp<2", "python",
                   "A:/GTN_Libraries/griptape-nodes-library-blender-bridge/blender_bridge_mcp.py"]
        }
      }
    }
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request

from mcp.server.fastmcp import FastMCP, Image

BRIDGE_URL = os.environ.get("GTN_BLENDER_BRIDGE_URL", "http://127.0.0.1:7890")
COMMAND_TIMEOUT = float(os.environ.get("GTN_BLENDER_MCP_TIMEOUT", "60"))

mcp = FastMCP(
    "blender-bridge",
    instructions=(
        "Tools that operate on the user's open Blender scene through the local "
        "Griptape bridge. Locations/rotations are [x, y, z] lists; rotations are "
        "radians. Use scene_info first to see what exists, and snapshot to see "
        "the scene through the camera after making changes."
    ),
)


def _get(path: str) -> dict:
    with urllib.request.urlopen(BRIDGE_URL + path, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE_URL + path, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _command(command: str, params: dict | None = None, timeout: float = COMMAND_TIMEOUT) -> dict:
    """Enqueue a command on the bridge and wait for Blender to execute it."""
    submitted = _post("/commands", {"command": command, "params": params or {}})
    command_id = submitted.get("id", "")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _get(f"/commands/{command_id}")
        status = state.get("status", "")
        if status == "done":
            return state.get("result") or {}
        if status == "error":
            raise RuntimeError(str(state.get("result") or "command failed"))
        time.sleep(0.25)
    raise TimeoutError(
        f"Blender did not execute '{command}' within {timeout:.0f}s. "
        "Is Blender open with the Griptape Bridge add-on and 'Listen for Commands' enabled?"
    )


@mcp.tool()
def scene_info() -> dict:
    """Get the Blender scene contents: objects (name/type/location), active camera, frame range, fps, resolution."""
    return _command("scene_info")


@mcp.tool()
def add_primitive(
    kind: str,
    name: str = "",
    location: list[float] | None = None,
    rotation_euler: list[float] | None = None,
    scale: list[float] | None = None,
) -> dict:
    """Add an object to the scene. kind: cube, sphere, plane, cylinder, cone, torus, empty, camera, light, sun, area_light."""
    params: dict = {"kind": kind}
    if name:
        params["name"] = name
    if location is not None:
        params["location"] = location
    if rotation_euler is not None:
        params["rotation_euler"] = rotation_euler
    if scale is not None:
        params["scale"] = scale
    return _command("add_primitive", params)


@mcp.tool()
def move_object(
    name: str,
    location: list[float] | None = None,
    rotation_euler: list[float] | None = None,
    scale: list[float] | None = None,
) -> dict:
    """Move/rotate/scale an existing object by name."""
    params: dict = {"name": name}
    if location is not None:
        params["location"] = location
    if rotation_euler is not None:
        params["rotation_euler"] = rotation_euler
    if scale is not None:
        params["scale"] = scale
    return _command("move_object", params)


@mcp.tool()
def delete_object(name: str) -> dict:
    """Delete an object from the scene by name."""
    return _command("delete_object", {"name": name})


@mcp.tool()
def get_camera(name: str = "") -> dict:
    """Get the transform and lens of a camera (active camera if name is empty)."""
    return _command("get_camera", {"name": name} if name else {})


@mcp.tool()
def set_camera(
    name: str = "",
    location: list[float] | None = None,
    rotation_euler: list[float] | None = None,
    lens_mm: float | None = None,
    keyframe: bool = False,
    frame: int | None = None,
) -> dict:
    """Set a camera's transform and lens. keyframe=True inserts location/rotation keys (at 'frame' or the current one)."""
    params: dict = {}
    if name:
        params["name"] = name
    if location is not None:
        params["location"] = location
    if rotation_euler is not None:
        params["rotation_euler"] = rotation_euler
    if lens_mm is not None:
        params["lens_mm"] = lens_mm
    if keyframe:
        params["keyframe"] = True
        if frame is not None:
            params["frame"] = frame
    return _command("set_camera", params)


@mcp.tool()
def set_frame(frame: int) -> dict:
    """Jump the timeline to a frame."""
    return _command("set_frame", {"frame": frame})


@mcp.tool()
def snapshot(camera: str = "", width: int = 960, height: int = 540) -> Image:
    """Render a fast OpenGL snapshot from a camera (active if empty) and return the image so you can see the scene."""
    params: dict = {"width": width, "height": height}
    if camera:
        params["camera"] = camera
    result = _command("snapshot", params, timeout=max(COMMAND_TIMEOUT, 120))
    data_url = result.get("image_data_url", "")
    if not data_url.startswith("data:image/png;base64,"):
        raise RuntimeError("Snapshot did not return an image.")
    return Image(data=base64.b64decode(data_url.split(",", 1)[1]), format="png")


@mcp.tool()
def run_python(code: str) -> dict:
    """Run Python inside Blender ('bpy' is available; assign to a variable named 'result' to return data).

    Requires 'Allow Python Commands' to be enabled in the Griptape Bridge add-on preferences.
    Use this for anything the other tools don't cover: materials, modifiers, curves, lights setup, etc.
    """
    return _command("run_python", {"code": code}, timeout=max(COMMAND_TIMEOUT, 120))


if __name__ == "__main__":
    mcp.run()
