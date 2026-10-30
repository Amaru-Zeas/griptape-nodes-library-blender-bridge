"""Blender Bridge node.

Receives images/videos that the Blender add-on uploads to the local bridge
server (with prompt + camera metadata), and sends GTN result media back to
Blender.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from griptape.artifacts import ImageArtifact, ImageUrlArtifact

try:
    from griptape.artifacts.video_url_artifact import VideoUrlArtifact
except Exception:  # pragma: no cover - older griptape versions
    VideoUrlArtifact = None  # type: ignore[assignment]

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.traits.widget import Widget

try:
    from . import bridge_server
except ImportError:  # pragma: no cover - fallback when loaded as top-level module
    import bridge_server  # type: ignore[no-redef]

try:
    from PIL import Image
except Exception:  # pragma: no cover - pillow is a declared dependency
    Image = None  # type: ignore[assignment]

_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
_VIDEO_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}
_MODEL_EXTENSIONS = {".glb", ".gltf", ".obj", ".fbx", ".usd", ".usdc", ".usda", ".usdz", ".stl", ".ply", ".bvh"}


class BlenderBridgeNode(DataNode):
    """Bridge images and videos between Blender and GTN."""

    def __init__(self, name: str, metadata: dict[str, Any] | None = None, **kwargs) -> None:
        node_metadata = {
            "category": "Integrations",
            "description": "Receive images/videos from Blender and send result media back.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name=name, metadata=node_metadata, **kwargs)

        try:
            bridge_server.ensure_server_started()
        except Exception:
            pass

        # Track button nonces so we can detect clicks in after_value_set.
        self._last_send_nonce = 0
        self._last_pull_nonce = 0

        self.add_parameter(
            Parameter(
                name="media_to_send",
                input_types=["ImageArtifact", "ImageUrlArtifact", "VideoArtifact", "VideoUrlArtifact", "dict", "str"],
                type="ImageArtifact",
                default_value=None,
                tooltip="Result media to send back to Blender (image/video artifact, URL/path, or data URL).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="bridge_widget",
                input_types=["dict"],
                type="dict",
                output_type="dict",
                default_value={
                    "port": bridge_server.get_active_port(),
                    "serverRunning": bridge_server.is_server_running(),
                    "incomingVersion": 0,
                    "incomingFilename": "",
                    "incomingKind": "image",
                    "incomingPreview": "",
                    "incomingPrompt": "",
                    "autoLoad": True,
                    "status": "Waiting for media from Blender.",
                    "sendNonce": 0,
                    "pullNonce": 0,
                },
                tooltip="Blender bridge status, preview, and send/pull controls.",
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
                traits={Widget(name="BlenderBridgeWidget", library="Blender Bridge Library")},
            )
        )
        self.add_parameter(
            Parameter(
                name="image",
                output_type="ImageArtifact",
                tooltip="Latest image received from Blender (empty if the latest media is a video).",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="video",
                output_type="VideoUrlArtifact",
                tooltip="Latest video received from Blender (empty if the latest media is an image).",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="media_path",
                output_type="str",
                tooltip="Served URL of the latest media received from Blender (best effort).",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="prompt",
                output_type="str",
                tooltip="Prompt text typed in the Blender panel when the media was sent.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="camera_info",
                output_type="dict",
                tooltip="Camera / scene metadata captured by Blender (transform, lens, resolution, frame).",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="status_message",
                output_type="str",
                tooltip="Bridge status.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _widget_state(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _save_static(data: bytes, filename: str) -> str:
        from griptape_nodes.retained_mode.griptape_nodes import (  # type: ignore[reportMissingImports]
            GriptapeNodes,
        )

        return GriptapeNodes.StaticFilesManager().save_static_file(data, filename)

    @staticmethod
    def _bytes_to_image_artifact(data: bytes) -> ImageArtifact:
        """Wrap raw image bytes in an ImageArtifact so GTN can render it inline."""
        width = 0
        height = 0
        payload = data
        if Image is not None:
            try:
                img = Image.open(io.BytesIO(data))
                width, height = img.width, img.height
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                out = io.BytesIO()
                img.save(out, format="PNG")
                payload = out.getvalue()
            except Exception:
                payload = data
        return ImageArtifact(value=payload, width=width, height=height, format="png")

    @staticmethod
    def _preview_data_url(data: bytes) -> str:
        """Build a small JPEG data URL for the widget preview."""
        if Image is None:
            return ""
        try:
            image = Image.open(io.BytesIO(data))
            if image.mode != "RGB":
                image = image.convert("RGB")
            max_width = 480
            if image.width > max_width:
                scale = max_width / float(image.width)
                image = image.resize(
                    (max_width, max(1, int(round(image.height * scale)))),
                    Image.Resampling.LANCZOS,
                )
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=72, optimize=True)
            encoded = base64.b64encode(out.getvalue()).decode("utf-8")
            return f"data:image/jpeg;base64,{encoded}"
        except Exception:
            return ""

    @staticmethod
    def _guess_kind_and_type(filename: str, default_kind: str = "image") -> tuple[str, str]:
        ext = Path(filename or "").suffix.lower()
        if ext in _VIDEO_EXTENSIONS:
            return "video", _VIDEO_CONTENT_TYPES.get(ext, "video/mp4")
        if ext in _MODEL_EXTENSIONS:
            return "model", "model/gltf-binary" if ext == ".glb" else "application/octet-stream"
        if ext in (".jpg", ".jpeg"):
            return "image", "image/jpeg"
        if ext == ".webp":
            return "image", "image/webp"
        if ext == ".png":
            return "image", "image/png"
        return default_kind, "video/mp4" if default_kind == "video" else "image/png"

    def _media_to_send_bytes(self) -> tuple[bytes, str]:
        """Normalize the ``media_to_send`` input into bytes + filename."""
        raw = self.parameter_values.get("media_to_send")
        if raw is None:
            return b"", ""

        if isinstance(raw, ImageArtifact):
            value = raw.value
            if isinstance(value, (bytes, bytearray)):
                return bytes(value), "gtn_result.png"

        if isinstance(raw, ImageUrlArtifact):
            return self._read_reference_bytes(str(raw.value or ""))

        if VideoUrlArtifact is not None and isinstance(raw, VideoUrlArtifact):
            return self._read_reference_bytes(str(raw.value or ""))

        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw), "gtn_result.png"

        if isinstance(raw, dict):
            for key in ("value", "url", "image_data_url", "path"):
                ref = raw.get(key)
                if isinstance(ref, str) and ref:
                    return self._read_reference_bytes(ref)

        if isinstance(raw, str):
            return self._read_reference_bytes(raw)

        value = getattr(raw, "value", None)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value), "gtn_result.png"
        if isinstance(value, str) and value:
            return self._read_reference_bytes(value)

        return b"", ""

    def _read_reference_bytes(self, reference: str) -> tuple[bytes, str]:
        reference = reference.strip()
        if not reference:
            return b"", ""

        if reference.startswith("data:"):
            try:
                header, b64 = reference.split(",", 1)
                data = base64.b64decode(b64)
                ext = "png"
                if "image/jpeg" in header or "image/jpg" in header:
                    ext = "jpg"
                elif "image/webp" in header:
                    ext = "webp"
                elif "video/mp4" in header:
                    ext = "mp4"
                elif "video/webm" in header:
                    ext = "webm"
                return data, f"gtn_result.{ext}"
            except Exception:
                return b"", ""

        parsed = urlparse(reference)
        filename = Path(parsed.path or reference).name or "gtn_result.png"
        try:
            from griptape_nodes.files.file import File  # type: ignore[reportMissingImports]

            data = File(reference).read_bytes()
            return data, filename
        except Exception:
            pass

        try:
            local = Path(reference)
            if local.exists():
                return local.read_bytes(), local.name or "gtn_result.png"
        except Exception:
            pass

        return b"", ""

    def _push_to_blender(self) -> str:
        data, filename = self._media_to_send_bytes()
        if not data:
            return "Nothing to send: connect or set 'media_to_send' first."
        kind, content_type = self._guess_kind_and_type(filename)
        try:
            slot = bridge_server.set_outbound(data=data, filename=filename, kind=kind, content_type=content_type)
            return f"Sent '{slot.filename}' ({slot.kind}) to Blender (v{slot.version})."
        except Exception as exc:  # pragma: no cover - defensive
            return f"Send to Blender failed: {exc}"

    def _load_inbound(self, widget_state: dict[str, Any]) -> dict[str, Any]:
        """Pull the latest Blender media into outputs + widget state."""
        slot = bridge_server.get_latest_inbound()
        widget_state["port"] = bridge_server.get_active_port()
        widget_state["serverRunning"] = bridge_server.is_server_running()

        if not slot.data:
            self.parameter_output_values["image"] = None
            self.parameter_output_values["video"] = None
            self.parameter_output_values["media_path"] = ""
            self.parameter_output_values["prompt"] = ""
            self.parameter_output_values["camera_info"] = {}
            self.parameter_output_values["status_message"] = "No media from Blender yet."
            widget_state["incomingVersion"] = 0
            widget_state["incomingFilename"] = ""
            widget_state["incomingKind"] = "image"
            widget_state["incomingPreview"] = ""
            widget_state["incomingPrompt"] = ""
            widget_state["status"] = "No media from Blender yet."
            return widget_state

        try:
            filename = slot.filename or ("blender_export.mp4" if slot.kind == "video" else "blender_export.png")
            meta = slot.meta if isinstance(slot.meta, dict) else {}

            served_url = ""
            try:
                served_url = self._save_static(slot.data, filename)
            except Exception:
                served_url = ""

            if slot.kind == "video":
                self.parameter_output_values["image"] = None
                if VideoUrlArtifact is not None and served_url:
                    self.parameter_output_values["video"] = VideoUrlArtifact(served_url)
                else:
                    self.parameter_output_values["video"] = None
                widget_state["incomingPreview"] = ""
            else:
                self.parameter_output_values["image"] = self._bytes_to_image_artifact(slot.data)
                self.parameter_output_values["video"] = None
                widget_state["incomingPreview"] = self._preview_data_url(slot.data)

            self.parameter_output_values["media_path"] = served_url
            self.parameter_output_values["prompt"] = str(meta.get("prompt", "") or "")
            self.parameter_output_values["camera_info"] = meta.get("camera", {}) or {}

            status = f"Loaded '{slot.filename}' ({slot.kind}) from Blender (v{slot.version})."
            self.parameter_output_values["status_message"] = status
            widget_state["incomingVersion"] = slot.version
            widget_state["incomingFilename"] = slot.filename
            widget_state["incomingKind"] = slot.kind
            widget_state["incomingPrompt"] = str(meta.get("prompt", "") or "")
            widget_state["status"] = status
        except Exception as exc:
            status = f"Failed to load Blender media: {exc}"
            self.parameter_output_values["status_message"] = status
            widget_state["status"] = status
        return widget_state

    # ------------------------------------------------------------------
    # GTN lifecycle
    # ------------------------------------------------------------------
    def process(self) -> None:
        widget_state = self._widget_state(self.parameter_values.get("bridge_widget", {}))

        widget_state = self._load_inbound(widget_state)

        # If media is wired to the send input, also push it on run so a plain
        # flow execution round-trips without needing the button.
        send_data, _ = self._media_to_send_bytes()
        if send_data:
            widget_state["status"] = self._push_to_blender()

        self.parameter_output_values["bridge_widget"] = widget_state

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        if parameter.name == "bridge_widget":
            widget_state = self._widget_state(value)

            send_nonce = int(widget_state.get("sendNonce", 0) or 0)
            pull_nonce = int(widget_state.get("pullNonce", 0) or 0)

            if send_nonce != self._last_send_nonce:
                self._last_send_nonce = send_nonce
                widget_state["status"] = self._push_to_blender()

            if pull_nonce != self._last_pull_nonce:
                self._last_pull_nonce = pull_nonce
                slot = bridge_server.get_latest_inbound()
                widget_state["incomingVersion"] = slot.version
                widget_state["incomingFilename"] = slot.filename
                widget_state["incomingKind"] = slot.kind
                if slot.data and slot.kind == "image":
                    widget_state["incomingPreview"] = self._preview_data_url(slot.data)
                else:
                    widget_state["incomingPreview"] = ""
                meta = slot.meta if isinstance(slot.meta, dict) else {}
                widget_state["incomingPrompt"] = str(meta.get("prompt", "") or "")
                widget_state["status"] = (
                    f"Media v{slot.version} ready - run node to load into the flow."
                    if slot.data
                    else "No media from Blender yet."
                )

            widget_state["port"] = bridge_server.get_active_port()
            widget_state["serverRunning"] = bridge_server.is_server_running()
            self.parameter_values["bridge_widget"] = widget_state

        return super().after_value_set(parameter, value)
