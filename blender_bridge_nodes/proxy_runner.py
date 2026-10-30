"""Run Griptape nodes as "proxies" for the Blender add-on.

The Blender panel asks for the catalog of registered node types, gets a
parameter schema for any node, and submits run jobs. This module services
those requests using the engine's retained-mode API:

* catalog  -- ListRegisteredLibraries / ListNodeTypesInLibrary / node metadata.
* schema   -- a hidden proxy flow holds one instance per node type; its live
              Parameter objects are serialized (type, default, choices, ...).
* run      -- SetParameterValue for each field, ResolveNode, poll until the
              node resolves, then collect outputs. Any image/video artifact
              found in the outputs is staged on the bridge's outbound slot so
              Blender's auto-receive places it in the scene.

Everything is defensive: if the engine API surface shifts between versions,
we return readable error strings to the Blender panel instead of crashing.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import traceback
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

PROXY_FLOW_NAME = "blender_proxy_flow"
CATALOG_TTL_SECONDS = 60.0
RUN_POLL_INTERVAL = 0.25

_lock = threading.RLock()
_flow_ready = False
_proxy_nodes: dict[str, str] = {}  # "library/node_type" -> node name in proxy flow
_catalog_cache: Optional[dict] = None
_catalog_cache_at = 0.0
_jobs: dict[str, dict] = {}
_job_serializer = threading.Lock()  # one node execution at a time


# ---------------------------------------------------------------------------
# Engine access
# ---------------------------------------------------------------------------
def _gtn():
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes  # type: ignore[import-not-found]

    return GriptapeNodes


def _events():
    import griptape_nodes.retained_mode.events.execution_events as execution_events  # type: ignore[import-not-found]
    import griptape_nodes.retained_mode.events.flow_events as flow_events  # type: ignore[import-not-found]
    import griptape_nodes.retained_mode.events.library_events as library_events  # type: ignore[import-not-found]
    import griptape_nodes.retained_mode.events.node_events as node_events  # type: ignore[import-not-found]
    import griptape_nodes.retained_mode.events.parameter_events as parameter_events  # type: ignore[import-not-found]

    return execution_events, flow_events, library_events, node_events, parameter_events


def _get_node_object(node_name: str) -> Any:
    obj_mgr = _gtn().ObjectManager()
    for getter in ("attempt_get_object_by_name", "get_object_by_name"):
        fn = getattr(obj_mgr, getter, None)
        if fn is not None:
            try:
                obj = fn(node_name)
                if obj is not None:
                    return obj
            except Exception:
                continue
    raise RuntimeError(f"Could not fetch node object '{node_name}' from the engine.")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def _load_curated() -> list[dict]:
    """User-editable whitelist in curated_nodes.json next to this module."""
    try:
        data = json.loads((Path(__file__).parent / "curated_nodes.json").read_text(encoding="utf-8"))
        entries = data.get("nodes", [])
        return [e for e in entries if isinstance(e, dict) and e.get("match")]
    except Exception:
        return []


def get_catalog(force: bool = False) -> dict:
    """Return {libraries: [{library, nodes: [{node_type, display_name, category, description}]}]}."""
    global _catalog_cache, _catalog_cache_at
    with _lock:
        if not force and _catalog_cache is not None and time.monotonic() - _catalog_cache_at < CATALOG_TTL_SECONDS:
            return _catalog_cache

        _execution_events, _flow_events, library_events, _node_events, _parameter_events = _events()
        gtn = _gtn()

        result = gtn.handle_request(library_events.ListRegisteredLibrariesRequest())
        libraries = list(getattr(result, "libraries", []) or [])

        catalog: list[dict] = []
        for library in libraries:
            nodes_result = gtn.handle_request(library_events.ListNodeTypesInLibraryRequest(library=library))
            node_types = list(getattr(nodes_result, "node_types", []) or [])
            entries = []
            for node_type in node_types:
                display_name = node_type
                category = ""
                description = ""
                try:
                    meta_result = gtn.handle_request(
                        library_events.GetNodeMetadataFromLibraryRequest(library=library, node_type=node_type)
                    )
                    meta = getattr(meta_result, "metadata", None)
                    if meta is not None:
                        display_name = getattr(meta, "display_name", None) or (
                            meta.get("display_name") if isinstance(meta, dict) else None
                        ) or node_type
                        category = getattr(meta, "category", None) or (
                            meta.get("category") if isinstance(meta, dict) else ""
                        ) or ""
                        description = getattr(meta, "description", None) or (
                            meta.get("description") if isinstance(meta, dict) else ""
                        ) or ""
                except Exception:
                    pass
                entries.append(
                    {
                        "node_type": node_type,
                        "display_name": str(display_name),
                        "category": str(category),
                        "description": str(description),
                    }
                )
            catalog.append({"library": library, "nodes": entries})

        # Annotate curated entries (whitelist by display name or node_type).
        curated = _load_curated()
        for lib in catalog:
            for entry in lib["nodes"]:
                for order, wanted in enumerate(curated):
                    match = str(wanted["match"])
                    if match in (entry["node_type"], entry["display_name"]):
                        entry["curated"] = True
                        entry["curated_order"] = order
                        if wanted.get("tab"):
                            entry["tab"] = str(wanted["tab"]).upper()
                        break

        _catalog_cache = {"libraries": catalog}
        _catalog_cache_at = time.monotonic()
        return _catalog_cache


# ---------------------------------------------------------------------------
# Proxy flow + node instances
# ---------------------------------------------------------------------------
def _ensure_proxy_flow() -> None:
    global _flow_ready
    if _flow_ready:
        return
    _execution_events, flow_events, _library_events, _node_events, _parameter_events = _events()
    gtn = _gtn()

    # Already exists? (e.g. library reload)
    try:
        obj_mgr = gtn.ObjectManager()
        fn = getattr(obj_mgr, "attempt_get_object_by_name", None)
        if fn is not None and fn(PROXY_FLOW_NAME) is not None:
            _flow_ready = True
            return
    except Exception:
        pass

    result = gtn.handle_request(
        flow_events.CreateFlowRequest(parent_flow_name=None, flow_name=PROXY_FLOW_NAME, set_as_new_context=False)
    )
    if getattr(result, "failed", None) and result.failed():
        # A root flow may already exist; try nesting under the first root flow.
        parent = None
        try:
            flows_result = gtn.handle_request(flow_events.ListFlowsInFlowRequest(parent_flow_name=None))
            flow_names = list(getattr(flows_result, "flow_names", []) or [])
            parent = flow_names[0] if flow_names else None
        except Exception:
            parent = None
        if parent is None:
            raise RuntimeError("Could not create the Blender proxy flow (no parent flow available).")
        result = gtn.handle_request(
            flow_events.CreateFlowRequest(parent_flow_name=parent, flow_name=PROXY_FLOW_NAME, set_as_new_context=False)
        )
        if getattr(result, "failed", None) and result.failed():
            raise RuntimeError("Could not create the Blender proxy flow.")
    _flow_ready = True


def _ensure_proxy_node(library: str, node_type: str) -> str:
    key = f"{library}/{node_type}"
    with _lock:
        if key in _proxy_nodes:
            # Confirm it still exists (workflow may have been reloaded).
            try:
                _get_node_object(_proxy_nodes[key])
                return _proxy_nodes[key]
            except Exception:
                _proxy_nodes.pop(key, None)

        _ensure_proxy_flow()
        _execution_events, _flow_events, _library_events, node_events, _parameter_events = _events()
        gtn = _gtn()

        node_name = f"BlenderProxy_{node_type}_{uuid.uuid4().hex[:6]}"
        result = gtn.handle_request(
            node_events.CreateNodeRequest(
                node_type=node_type,
                specific_library_name=library or None,
                node_name=node_name,
                override_parent_flow_name=PROXY_FLOW_NAME,
            )
        )
        if getattr(result, "failed", None) and result.failed():
            raise RuntimeError(f"Could not create proxy node for '{node_type}' (library '{library}').")
        created_name = getattr(result, "node_name", None) or node_name
        _proxy_nodes[key] = created_name
        return created_name


# ---------------------------------------------------------------------------
# Schema extraction
# ---------------------------------------------------------------------------
_MEDIA_TYPE_HINTS = (
    "imageartifact",
    "imageurlartifact",
    "videoartifact",
    "videourlartifact",
    "audioartifact",
    "3durlartifact",
    "threedurlartifact",
    "modelartifact",
    "modelurlartifact",
    "gltfartifact",
    "meshartifact",
)


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v, depth + 1) for v in value[:50]]
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in list(value.items())[:50]}
    inner = getattr(value, "value", None)
    if isinstance(inner, str):
        return inner
    return str(value)


def _param_choices(param: Any) -> Optional[list]:
    try:
        from griptape_nodes.traits.options import Options  # type: ignore[import-not-found]

        finder = getattr(param, "find_elements_by_type", None)
        if finder is not None:
            options = finder(Options)
            if options:
                choices = getattr(options[0], "choices", None)
                if choices:
                    return [_json_safe(c) for c in choices]
    except Exception:
        pass
    return None


def _param_modes(param: Any) -> set[str]:
    modes = set()
    for mode in getattr(param, "allowed_modes", set()) or set():
        modes.add(str(mode).rsplit(".", 1)[-1].upper())
    return modes


def get_schema(library: str, node_type: str) -> dict:
    """Serialize the editable parameters of a node type."""
    node_name = _ensure_proxy_node(library, node_type)
    node = _get_node_object(node_name)

    params_out: list[dict] = []
    for param in getattr(node, "parameters", []) or []:
        try:
            name = getattr(param, "name", "")
            if not name:
                continue
            type_str = str(getattr(param, "type", "") or "")
            input_types = [str(t) for t in (getattr(param, "input_types", None) or [])]
            all_types = " ".join([type_str, *input_types]).lower()
            if "parametercontroltype" in all_types or "control" == type_str.lower():
                continue

            modes = _param_modes(param)
            if not ({"INPUT", "PROPERTY"} & modes):
                continue  # output-only

            is_media = any(hint in all_types for hint in _MEDIA_TYPE_HINTS)
            choices = _param_choices(param)

            ui_options = getattr(param, "ui_options", None) or {}
            tooltip = getattr(param, "tooltip", "") or ""
            if not isinstance(tooltip, str):
                tooltip = ""

            base_type = type_str.lower()
            if choices:
                ui_type = "enum"
            elif is_media:
                ui_type = "media"
            elif base_type in ("int",):
                ui_type = "int"
            elif base_type in ("float",):
                ui_type = "float"
            elif base_type in ("bool",):
                ui_type = "bool"
            elif base_type in ("str", "string"):
                ui_type = "string"
            else:
                ui_type = "json"

            params_out.append(
                {
                    "name": name,
                    "label": str(ui_options.get("display_name", "") or name),
                    "type": type_str,
                    "ui_type": ui_type,
                    "default": _json_safe(getattr(param, "default_value", None)),
                    "tooltip": tooltip,
                    "choices": choices,
                    "multiline": bool(ui_options.get("multiline", False)),
                    "modes": sorted(modes),
                }
            )
        except Exception:
            continue

    return {"library": library, "node_type": node_type, "node_name": node_name, "params": params_out}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _save_static(data: bytes, filename: str) -> str:
    return _gtn().StaticFilesManager().save_static_file(data, filename)


def _stage_inbound_as_url() -> str:
    """Save the latest media Blender sent to the bridge as a static file URL."""
    try:
        from . import bridge_server
    except ImportError:  # pragma: no cover
        import bridge_server  # type: ignore[no-redef]

    slot = bridge_server.get_latest_inbound()
    if not slot.data:
        raise RuntimeError("No capture on the bridge. Send a viewport/camera capture from Blender first.")
    return _save_static(slot.data, slot.filename or "blender_capture.png")


_MODEL_EXTS = (".glb", ".gltf", ".obj", ".fbx", ".usd", ".usdc", ".usda", ".usdz", ".stl", ".ply")


def _kind_for_reference(reference: str, fallback: str) -> str:
    name = reference.rstrip("/").rsplit("/", 1)[-1].split("?")[0].lower()
    if name.endswith(_MODEL_EXTS):
        return "model"
    if name.endswith((".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")):
        return "video"
    if name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return "image"
    return fallback


def _resolve_engine_location(reference: str) -> str:
    """Resolve engine file references to something readable.

    Node outputs often hold macro templates like ``{outputs}/tripo_model.glb``
    or workspace-relative paths rather than absolute paths/URLs. The engine's
    File API resolves those (and maps localhost static URLs to local paths).
    """
    try:
        from griptape_nodes.files.file import File

        resolved = File(reference).resolve()
        if resolved:
            return resolved
    except Exception:
        pass
    return reference


def _fallback_kind_for_class(class_name: str) -> str:
    if "video" in class_name:
        return "video"
    if any(hint in class_name for hint in ("3d", "model", "gltf", "mesh")):
        return "model"
    return "image"


def _artifact_bytes(value: Any) -> tuple[bytes, str, str]:
    """Best-effort conversion of an output artifact/URL/path to (bytes, filename, kind)."""
    class_name = type(value).__name__.lower()
    if isinstance(value, str):
        inner: Any = value
    elif isinstance(value, dict):
        inner = value.get("value") or value.get("url") or value.get("path") or ""
    else:
        inner = getattr(value, "value", None)

    if isinstance(inner, (bytes, bytearray)):
        kind = _fallback_kind_for_class(class_name)
        ext = {"video": "mp4", "model": "glb"}.get(kind, "png")
        return bytes(inner), f"gtn_result.{ext}", kind

    if not isinstance(inner, str) or not inner:
        return b"", "", ""

    fallback = _fallback_kind_for_class(class_name)
    kind = _kind_for_reference(inner, fallback)
    default_ext = {"video": "mp4", "model": "glb"}.get(kind, "png")

    resolved = _resolve_engine_location(inner)

    if resolved.startswith(("http://", "https://", "file://")):
        try:
            with urllib.request.urlopen(resolved, timeout=120) as resp:
                data = resp.read()
            name = resolved.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or f"gtn_result.{default_ext}"
            return data, name, kind
        except Exception:
            return b"", "", ""

    # Local file path (macro templates and workspace paths resolve to these).
    try:
        local = Path(resolved)
        if local.is_file():
            return local.read_bytes(), local.name, _kind_for_reference(local.name, fallback)
    except Exception:
        pass

    return b"", "", ""


def _collect_outputs(node: Any) -> tuple[dict, Optional[dict]]:
    """Return (json_safe_outputs, staged_media_info)."""
    outputs = getattr(node, "parameter_output_values", {}) or {}
    json_outputs: dict = {}
    staged: Optional[dict] = None

    try:
        from . import bridge_server
    except ImportError:  # pragma: no cover
        import bridge_server  # type: ignore[no-redef]

    _content_types = {"video": "video/mp4", "model": "model/gltf-binary", "image": "image/png"}

    # Convert every media-looking output first, then stage the best one:
    # models beat videos beat images (a 3D node often also outputs a preview
    # image, and the mesh is what belongs in the scene).
    candidates: list[tuple[str, bytes, str, str]] = []  # (param, data, filename, kind)

    for key, value in outputs.items():
        class_name = type(value).__name__.lower()
        is_media_artifact = any(hint in class_name for hint in _MEDIA_TYPE_HINTS)
        is_media_reference = isinstance(value, str) and _kind_for_reference(value, "") in ("image", "video", "model")

        if is_media_artifact or is_media_reference:
            data, filename, kind = _artifact_bytes(value)
            if data:
                candidates.append((key, data, filename, kind))
            json_outputs[key] = _json_safe(getattr(value, "value", str(value)) if not isinstance(value, str) else value)
        else:
            json_outputs[key] = _json_safe(value)

    if candidates:
        priority = {"model": 0, "video": 1, "image": 2}
        candidates.sort(key=lambda c: priority.get(c[3], 3))
        key, data, filename, kind = candidates[0]

        # 3D nodes usually also render a textured preview image; ship it
        # along as a thumbnail so the Blender asset browser can show the
        # actual object instead of a generic cube icon.
        meta: dict = {}
        if kind == "model":
            preview = next((c for c in candidates if c[3] == "image"), None)
            if preview is not None and len(preview[1]) < 8 * 1024 * 1024:
                ext = "." + (preview[2].rsplit(".", 1)[-1].lower() if "." in preview[2] else "webp")
                meta["thumbB64"] = base64.b64encode(preview[1]).decode("ascii")
                meta["thumbExt"] = ext

        slot = bridge_server.set_outbound(
            data=data,
            filename=filename,
            kind=kind,
            content_type=_content_types.get(kind, "application/octet-stream"),
            meta=meta,
        )
        staged = {"parameter": key, "filename": slot.filename, "kind": kind, "version": slot.version}

    return json_outputs, staged


def _node_is_resolved(node: Any) -> bool:
    state = getattr(node, "state", None)
    return "RESOLVED" == str(state).rsplit(".", 1)[-1].upper()


def _execute(job: dict) -> None:
    execution_events, _flow_events, _library_events, _node_events, parameter_events = _events()
    gtn = _gtn()

    library = job["library"]
    node_type = job["node_type"]
    params: dict = job.get("params") or {}
    attach_param: str = job.get("attach_param") or ""
    timeout = float(job.get("timeout", 600.0) or 600.0)

    node_name = _ensure_proxy_node(library, node_type)
    node = _get_node_object(node_name)

    if attach_param:
        url = _stage_inbound_as_url()
        params = dict(params)
        params[attach_param] = url

    for key, value in params.items():
        result = gtn.handle_request(
            parameter_events.SetParameterValueRequest(parameter_name=key, value=value, node_name=node_name)
        )
        if getattr(result, "failed", None) and result.failed():
            raise RuntimeError(f"Could not set parameter '{key}' on '{node_type}'.")

    result = gtn.handle_request(execution_events.ResolveNodeRequest(node_name=node_name))
    if getattr(result, "failed", None) and result.failed():
        exceptions = getattr(result, "validation_exceptions", None) or []
        detail = "; ".join(str(e) for e in exceptions) or "validation failed"
        raise RuntimeError(f"Could not start '{node_type}': {detail}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _node_is_resolved(node):
            break
        time.sleep(RUN_POLL_INTERVAL)
    else:
        raise RuntimeError(f"'{node_type}' did not finish within {timeout:.0f}s.")

    outputs, staged = _collect_outputs(node)
    job["result"] = {"outputs": outputs, "staged_media": staged, "node_name": node_name}


def _job_worker(job_id: str) -> None:
    job = _jobs.get(job_id)
    if job is None:
        return
    with _job_serializer:
        job["status"] = "running"
        try:
            _execute(job)
            job["status"] = "done"
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            job["traceback"] = traceback.format_exc()


def submit_job(library: str, node_type: str, params: dict, attach_param: str = "", timeout: float = 600.0) -> str:
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {
        "id": job_id,
        "library": library,
        "node_type": node_type,
        "params": params,
        "attach_param": attach_param,
        "timeout": timeout,
        "status": "queued",
        "result": None,
        "error": "",
    }
    thread = threading.Thread(target=_job_worker, args=(job_id,), name=f"gtn-blender-proxy-{job_id[:6]}", daemon=True)
    thread.start()
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    job = _jobs.get(job_id)
    if job is None:
        return None
    return {
        "id": job["id"],
        "status": job["status"],
        "result": job["result"],
        "error": job.get("error", ""),
    }
