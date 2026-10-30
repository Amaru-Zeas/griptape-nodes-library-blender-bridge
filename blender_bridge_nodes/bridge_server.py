"""Local HTTP bridge server shared by the Blender Bridge nodes and add-on.

A single ``ThreadingHTTPServer`` runs in a daemon thread inside the GTN engine
process. It holds:

* inbound media  -- pushed by the Blender add-on, consumed by GTN nodes.
* outbound media -- pushed by GTN nodes, consumed by the Blender add-on.
* a command queue -- GTN nodes (or agents / MCP tools) enqueue scene commands,
  the Blender add-on polls for them, executes on Blender's main thread, and
  posts results back.

The GTN nodes talk to the store directly (in-process); Blender and the GTN web
UI widget talk to it over HTTP on ``127.0.0.1``.

Media is transported as raw bytes with metadata in headers:

* ``X-Filename``  -- original filename.
* ``X-Kind``      -- ``image`` or ``video``.
* ``X-Meta``      -- base64-encoded UTF-8 JSON (prompt, camera, scene info...).
"""

from __future__ import annotations

import base64
import json
import os
import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse


def _proxy_runner():
    """Lazy import so the media/command endpoints work even outside the engine."""
    try:
        from . import proxy_runner
    except ImportError:  # pragma: no cover - fallback when loaded as top-level module
        import proxy_runner  # type: ignore[no-redef]
    return proxy_runner


def _agent_runner():
    """Lazy import with the same top-level fallback as _proxy_runner."""
    try:
        from . import agent_runner
    except ImportError:  # pragma: no cover - fallback when loaded as top-level module
        import agent_runner  # type: ignore[no-redef]
    return agent_runner

DEFAULT_PORT = 7890
HOST = "127.0.0.1"
# Bumped when the bridge API changes; the Blender add-on warns on mismatch so
# "engine still running old code" failures stop being silent.
BRIDGE_VERSION = "0.6.6"
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB cap (videos can be big)
MAX_FINISHED_COMMANDS = 64


# ---------------------------------------------------------------------------
# Media slots
# ---------------------------------------------------------------------------
@dataclass
class MediaSlot:
    """Most recent media payload for one direction of the bridge."""

    version: int = 0
    data: bytes = b""
    filename: str = ""
    kind: str = "image"  # "image" | "video"
    content_type: str = "image/png"
    meta: dict = field(default_factory=dict)
    updated_at: str = ""

    def metadata(self) -> dict:
        return {
            "version": self.version,
            "filename": self.filename,
            "kind": self.kind,
            "contentType": self.content_type,
            "hasMedia": bool(self.data),
            "size": len(self.data),
            "meta": self.meta,
            "updatedAt": self.updated_at,
        }


class _BridgeStore:
    """Thread-safe holder for media slots and the command queue."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._inbound = MediaSlot()
        self._outbound = MediaSlot()
        self._queued: deque[dict] = deque()
        self._commands: OrderedDict[str, dict] = OrderedDict()

    # -- media ----------------------------------------------------------
    def _make_slot(self, previous: MediaSlot, data: bytes, filename: str, kind: str, content_type: str, meta: dict) -> MediaSlot:
        return MediaSlot(
            version=previous.version + 1,
            data=data,
            filename=filename,
            kind=kind if kind in ("image", "video", "model") else "image",
            content_type=content_type,
            meta=meta if isinstance(meta, dict) else {},
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def set_inbound(self, data: bytes, filename: str, kind: str, content_type: str, meta: dict) -> MediaSlot:
        with self._lock:
            self._inbound = self._make_slot(
                self._inbound, data, filename or "blender_export.png", kind, content_type or "image/png", meta
            )
            return self._inbound

    def set_outbound(self, data: bytes, filename: str, kind: str, content_type: str, meta: dict) -> MediaSlot:
        with self._lock:
            self._outbound = self._make_slot(
                self._outbound, data, filename or "gtn_result.png", kind, content_type or "image/png", meta
            )
            return self._outbound

    def get_inbound(self) -> MediaSlot:
        with self._lock:
            return self._inbound

    def get_outbound(self) -> MediaSlot:
        with self._lock:
            return self._outbound

    # -- commands ---------------------------------------------------------
    def enqueue_command(self, command: str, params: dict) -> dict:
        with self._lock:
            entry = {
                "id": uuid.uuid4().hex,
                "command": str(command),
                "params": params if isinstance(params, dict) else {},
                "status": "queued",
                "result": None,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
            self._queued.append(entry)
            self._commands[entry["id"]] = entry
            self._trim_finished()
            return entry

    def pop_next_command(self) -> Optional[dict]:
        """Hand the oldest queued command to Blender (marks it running)."""
        with self._lock:
            while self._queued:
                entry = self._queued.popleft()
                if entry["status"] == "queued":
                    entry["status"] = "running"
                    return entry
            return None

    def set_command_result(self, command_id: str, ok: bool, result: Any) -> Optional[dict]:
        with self._lock:
            entry = self._commands.get(command_id)
            if entry is None:
                return None
            entry["status"] = "done" if ok else "error"
            entry["result"] = result
            entry["finishedAt"] = datetime.now(timezone.utc).isoformat()
            return entry

    def get_command(self, command_id: str) -> Optional[dict]:
        with self._lock:
            return self._commands.get(command_id)

    def _trim_finished(self) -> None:
        finished = [cid for cid, entry in self._commands.items() if entry["status"] in ("done", "error")]
        excess = len(finished) - MAX_FINISHED_COMMANDS
        for cid in finished[: max(0, excess)]:
            self._commands.pop(cid, None)


_STORE = _BridgeStore()


# ---------------------------------------------------------------------------
# In-process helpers (used directly by the GTN nodes).
# ---------------------------------------------------------------------------
def get_latest_inbound() -> MediaSlot:
    """Return the most recent media uploaded by Blender."""
    return _STORE.get_inbound()


def set_outbound(
    data: bytes,
    filename: str = "gtn_result.png",
    kind: str = "image",
    content_type: str = "image/png",
    meta: Optional[dict] = None,
) -> MediaSlot:
    """Stage media for the Blender add-on to pick up."""
    return _STORE.set_outbound(data=data, filename=filename, kind=kind, content_type=content_type, meta=meta or {})


def get_latest_outbound() -> MediaSlot:
    return _STORE.get_outbound()


def enqueue_command(command: str, params: Optional[dict] = None) -> dict:
    return _STORE.enqueue_command(command, params or {})


def get_command(command_id: str) -> Optional[dict]:
    return _STORE.get_command(command_id)


def cancel_queued_commands() -> int:
    """Mark all still-queued commands as cancelled so Blender never runs them."""
    cancelled = 0
    with _STORE._lock:
        while _STORE._queued:
            entry = _STORE._queued.popleft()
            if entry["status"] == "queued":
                entry["status"] = "error"
                entry["result"] = {"error": "cancelled by user"}
                cancelled += 1
    return cancelled


# ---------------------------------------------------------------------------
# HTTP layer.
# ---------------------------------------------------------------------------
def _decode_meta_header(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return {}


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "GTNBlenderBridge/0.1"

    def log_message(self, *_args) -> None:  # noqa: D401 - quiet default logging
        return

    def _set_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename, X-Kind, X-Meta")

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._set_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self._set_cors()
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._set_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send_json({"ok": True, "service": "gtn-blender-bridge", "version": BRIDGE_VERSION})
            return
        if path == "/auth/status":
            try:
                from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

                key = GriptapeNodes.SecretsManager().get_secret("GT_CLOUD_API_KEY", should_error_on_not_found=False)
                hint = f"...{key[-4:]}" if key and len(key) >= 8 else ""
                self._send_json({"ok": True, "signed_in": bool(key), "key_hint": hint})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        if path == "/proxy/nodes":
            try:
                force = (query.get("force", ["false"])[0] or "false").lower() == "true"
                self._send_json({"ok": True, **_proxy_runner().get_catalog(force=force)})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        if path == "/proxy/schema":
            try:
                library = (query.get("library", [""])[0] or "").strip()
                node_type = (query.get("node_type", [""])[0] or "").strip()
                if not node_type:
                    self._send_json({"ok": False, "error": "missing node_type"}, status=400)
                    return
                self._send_json({"ok": True, **_proxy_runner().get_schema(library, node_type)})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        if path == "/agent/models":
            try:
                from griptape_nodes.drivers.cloud_models import MODEL_CHOICES

                models = [str(m) for m in MODEL_CHOICES]
            except Exception:
                models = []
            self._send_json({"ok": True, "models": models})
            return
        if path.startswith("/agent/jobs/"):
            try:
                job = _agent_runner().get_agent_job(path.rsplit("/", 1)[-1])
            except Exception:
                job = None
            if job is None:
                self._send_json({"ok": False, "error": "unknown_job"}, status=404)
            else:
                self._send_json({"ok": True, **job})
            return
        if path.startswith("/proxy/jobs/"):
            job = _proxy_runner().get_job(path.rsplit("/", 1)[-1])
            if job is None:
                self._send_json({"ok": False, "error": "unknown_job"}, status=404)
            else:
                self._send_json({"ok": True, **job})
            return
        if path == "/to_gtn/latest":
            self._send_json(_STORE.get_inbound().metadata())
            return
        if path == "/to_gtn/latest/raw":
            slot = _STORE.get_inbound()
            if not slot.data:
                self._send_json({"error": "no_media"}, status=404)
                return
            self._send_bytes(slot.data, slot.content_type)
            return
        if path == "/to_blender/latest":
            self._send_json(_STORE.get_outbound().metadata())
            return
        if path == "/to_blender/latest/raw":
            slot = _STORE.get_outbound()
            if not slot.data:
                self._send_json({"error": "no_media"}, status=404)
                return
            self._send_bytes(slot.data, slot.content_type)
            return
        if path == "/commands/next":
            entry = _STORE.pop_next_command()
            if entry is None:
                self._send_json({"ok": True, "command": None})
            else:
                self._send_json(
                    {"ok": True, "command": {"id": entry["id"], "command": entry["command"], "params": entry["params"]}}
                )
            return
        if path.startswith("/commands/"):
            command_id = path.rsplit("/", 1)[-1]
            entry = _STORE.get_command(command_id)
            if entry is None:
                self._send_json({"error": "unknown_command"}, status=404)
            else:
                self._send_json(
                    {"ok": True, "id": entry["id"], "status": entry["status"], "result": entry["result"]}
                )
            return

        self._send_json({"error": "not_found", "path": path}, status=404)

    def _read_body(self) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_UPLOAD_BYTES:
            return None
        remaining = length
        chunks = []
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/to_gtn", "/to_blender"):
            data = self._read_body()
            if data is None:
                self._send_json({"error": "payload_too_large"}, status=413)
                return
            if not data:
                self._send_json({"error": "empty_body"}, status=400)
                return

            filename = self.headers.get("X-Filename", "").strip()
            kind = self.headers.get("X-Kind", "image").strip().lower()
            content_type = self.headers.get("Content-Type", "").strip() or (
                "video/mp4" if kind == "video" else "image/png"
            )
            meta = _decode_meta_header(self.headers.get("X-Meta", "").strip())

            if path == "/to_gtn":
                slot = _STORE.set_inbound(data=data, filename=filename, kind=kind, content_type=content_type, meta=meta)
            else:
                slot = _STORE.set_outbound(data=data, filename=filename, kind=kind, content_type=content_type, meta=meta)
            self._send_json({"ok": True, **slot.metadata()})
            return

        if path == "/commands":
            body = self._read_body() or b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._send_json({"error": "invalid_json"}, status=400)
                return
            command = str(payload.get("command", "")).strip()
            if not command:
                self._send_json({"error": "missing_command"}, status=400)
                return
            entry = _STORE.enqueue_command(command, payload.get("params") or {})
            self._send_json({"ok": True, "id": entry["id"], "status": entry["status"]})
            return

        if path == "/proxy/run":
            body = self._read_body() or b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._send_json({"error": "invalid_json"}, status=400)
                return
            node_type = str(payload.get("node_type", "")).strip()
            if not node_type:
                self._send_json({"error": "missing_node_type"}, status=400)
                return
            try:
                job_id = _proxy_runner().submit_job(
                    library=str(payload.get("library", "")).strip(),
                    node_type=node_type,
                    params=payload.get("params") or {},
                    attach_param=str(payload.get("attach_param", "") or ""),
                    timeout=float(payload.get("timeout", 600.0) or 600.0),
                )
                self._send_json({"ok": True, "id": job_id})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path == "/agent/run":
            body = self._read_body() or b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._send_json({"error": "invalid_json"}, status=400)
                return
            try:
                job_id = _agent_runner().submit_agent_job(
                    prompt=str(payload.get("prompt", "")),
                    model=str(payload.get("model", "") or ""),
                )
                self._send_json({"ok": True, "id": job_id})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path == "/agent/cancel":
            body = self._read_body() or b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {}
            try:
                found = _agent_runner().cancel_agent_job(str(payload.get("id", "")))
            except Exception:
                found = False
            self._send_json({"ok": bool(found)})
            return

        if path == "/auth/login":
            body = self._read_body() or b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._send_json({"error": "invalid_json"}, status=400)
                return
            api_key = str(payload.get("api_key", "")).strip()
            if not api_key:
                self._send_json({"error": "missing_api_key"}, status=400)
                return
            try:
                from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

                # Persists to the global .env and reloads it, so nodes pick the
                # key up immediately -- no engine restart.
                GriptapeNodes.SecretsManager().set_secret("GT_CLOUD_API_KEY", api_key)
                self._send_json({"ok": True, "signed_in": True})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path == "/commands/result":
            body = self._read_body() or b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._send_json({"error": "invalid_json"}, status=400)
                return
            command_id = str(payload.get("id", "")).strip()
            entry = _STORE.set_command_result(command_id, bool(payload.get("ok", False)), payload.get("result"))
            if entry is None:
                self._send_json({"error": "unknown_command"}, status=404)
            else:
                self._send_json({"ok": True, "id": entry["id"], "status": entry["status"]})
            return

        self._send_json({"error": "not_found", "path": path}, status=404)


# ---------------------------------------------------------------------------
# Server lifecycle (idempotent singleton).
# ---------------------------------------------------------------------------
_SERVER_LOCK = threading.RLock()
_SERVER: Optional[ThreadingHTTPServer] = None
_SERVER_THREAD: Optional[threading.Thread] = None
_ACTIVE_PORT: Optional[int] = None


def _configured_port() -> int:
    raw = os.environ.get("GTN_BLENDER_BRIDGE_PORT", "").strip()
    if raw:
        try:
            value = int(raw)
            if 1 <= value <= 65535:
                return value
        except ValueError:
            pass
    return DEFAULT_PORT


def ensure_server_started() -> int:
    """Start the bridge server if it isn't already running. Returns the port."""
    global _SERVER, _SERVER_THREAD, _ACTIVE_PORT

    with _SERVER_LOCK:
        if _SERVER is not None and _SERVER_THREAD is not None and _SERVER_THREAD.is_alive():
            return _ACTIVE_PORT or _configured_port()

        port = _configured_port()
        try:
            server = ThreadingHTTPServer((HOST, port), _BridgeHandler)
        except OSError:
            # Port is busy. Assume another instance of this server already owns
            # it (e.g. a reloaded library) and reuse it rather than crashing.
            _ACTIVE_PORT = port
            return port

        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="gtn-blender-bridge",
            daemon=True,
        )
        thread.start()

        _SERVER = server
        _SERVER_THREAD = thread
        _ACTIVE_PORT = port
        return port


def get_active_port() -> int:
    return _ACTIVE_PORT or _configured_port()


def is_server_running() -> bool:
    with _SERVER_LOCK:
        return _SERVER is not None and _SERVER_THREAD is not None and _SERVER_THREAD.is_alive()
