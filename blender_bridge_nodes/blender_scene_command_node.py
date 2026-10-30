"""Blender Scene Command node.

Enqueues a scene command on the bridge, waits for the Blender add-on to
execute it (the add-on polls the bridge and runs commands on Blender's main
thread), then exposes the result.

This is the hook that lets GTN flows and Griptape agents drive the open
Blender scene: add objects, move cameras, take snapshots, import generated
media as planes, and (if enabled in the add-on preferences) run arbitrary
Python for full asset-assembly control.
"""

from __future__ import annotations

import base64
import io
import json
import time
from typing import Any

from griptape.artifacts import ImageArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.traits.options import Options

try:
    from . import bridge_server
except ImportError:  # pragma: no cover - fallback when loaded as top-level module
    import bridge_server  # type: ignore[no-redef]

try:
    from PIL import Image
except Exception:  # pragma: no cover - pillow is a declared dependency
    Image = None  # type: ignore[assignment]

COMMAND_CHOICES = [
    "scene_info",
    "list_cameras",
    "get_camera",
    "set_camera",
    "snapshot",
    "add_primitive",
    "move_object",
    "delete_object",
    "import_latest_as_plane",
    "set_frame",
    "run_python",
]


class BlenderSceneCommandNode(ControlNode):
    """Send a scene command to Blender through the bridge and wait for the result."""

    def __init__(self, name: str, metadata: dict[str, Any] | None = None, **kwargs) -> None:
        node_metadata = {
            "category": "Integrations",
            "description": "Run a scene command inside Blender (snapshot, camera, objects, python).",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name=name, metadata=node_metadata, **kwargs)

        try:
            bridge_server.ensure_server_started()
        except Exception:
            pass

        self.add_parameter(
            Parameter(
                name="command",
                input_types=["str"],
                type="str",
                default_value="scene_info",
                tooltip="Command for the Blender add-on to execute ('Listen for Commands' must be on in Blender).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                traits={Options(choices=COMMAND_CHOICES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="params",
                input_types=["dict", "str"],
                type="dict",
                default_value=None,
                tooltip="Optional command parameters as a dict (or JSON string). Overrides params_json.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="params_json",
                input_types=["str"],
                type="str",
                default_value="{}",
                tooltip='Command parameters as JSON, e.g. {"kind": "cube", "location": [0, 0, 1]}.',
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"multiline": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="timeout_seconds",
                input_types=["float", "int"],
                type="float",
                default_value=30.0,
                tooltip="How long to wait for Blender to pick up and finish the command.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                output_type="dict",
                tooltip="Raw result payload returned by the Blender add-on.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="succeeded",
                output_type="bool",
                tooltip="True when the command executed without errors.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="image",
                output_type="ImageArtifact",
                tooltip="Image output when the command returns one (e.g. snapshot).",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="status_message",
                output_type="str",
                tooltip="Command status.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _resolve_params(self) -> dict:
        raw = self.parameter_values.get("params")
        if isinstance(raw, dict) and raw:
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

        raw_json = self.parameter_values.get("params_json")
        if isinstance(raw_json, str) and raw_json.strip():
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    return parsed
            except Exception as exc:
                raise ValueError(f"params_json is not valid JSON: {exc}") from exc
        return {}

    @staticmethod
    def _image_from_result(result: Any) -> ImageArtifact | None:
        if not isinstance(result, dict):
            return None
        data_url = result.get("image_data_url")
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            return None
        try:
            _, b64 = data_url.split(",", 1)
            data = base64.b64decode(b64)
        except Exception:
            return None
        width = 0
        height = 0
        if Image is not None:
            try:
                img = Image.open(io.BytesIO(data))
                width, height = img.width, img.height
            except Exception:
                pass
        return ImageArtifact(value=data, width=width, height=height, format="png")

    # ------------------------------------------------------------------
    # GTN lifecycle
    # ------------------------------------------------------------------
    def process(self) -> None:
        command = str(self.parameter_values.get("command", "scene_info") or "scene_info").strip()
        try:
            timeout = float(self.parameter_values.get("timeout_seconds", 30.0) or 30.0)
        except (TypeError, ValueError):
            timeout = 30.0
        timeout = max(1.0, min(timeout, 600.0))

        self.parameter_output_values["result"] = {}
        self.parameter_output_values["succeeded"] = False
        self.parameter_output_values["image"] = None

        try:
            params = self._resolve_params()
        except ValueError as exc:
            self.parameter_output_values["status_message"] = str(exc)
            return

        bridge_server.ensure_server_started()
        entry = bridge_server.enqueue_command(command, params)
        command_id = entry["id"]

        deadline = time.monotonic() + timeout
        status = "queued"
        result: Any = None
        while time.monotonic() < deadline:
            current = bridge_server.get_command(command_id)
            if current is None:
                break
            status = current["status"]
            if status in ("done", "error"):
                result = current["result"]
                break
            time.sleep(0.2)

        if status == "done":
            self.parameter_output_values["result"] = result if isinstance(result, dict) else {"value": result}
            self.parameter_output_values["succeeded"] = True
            self.parameter_output_values["image"] = self._image_from_result(result)
            self.parameter_output_values["status_message"] = f"'{command}' completed."
        elif status == "error":
            self.parameter_output_values["result"] = result if isinstance(result, dict) else {"error": result}
            self.parameter_output_values["status_message"] = f"'{command}' failed in Blender: {result}"
        else:
            self.parameter_output_values["status_message"] = (
                f"'{command}' timed out after {timeout:.0f}s. Is Blender open with the Griptape add-on "
                "and 'Listen for Commands' enabled?"
            )
