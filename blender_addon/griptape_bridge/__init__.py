"""Griptape Bridge - Blender add-on.

Talks to the bridge server that runs inside the Griptape Nodes engine
(see the "Blender Bridge Library" GTN library). Blender is a pure HTTP
client: no server, no threads, no Docker on this side.

* Send: viewport / camera captures, image files, video files -> GTN,
  together with the prompt and camera metadata.
* Receive: a timer polls the bridge and places new media into the scene
  (image editor, plane at cursor, camera background, or VSE strip).
* Commands: when "Listen for Commands" is on, the same timer pulls scene
  commands queued by GTN flows / agents and executes them on Blender's
  main thread (add objects, move cameras, snapshots, optional Python).
"""

bl_info = {
    "name": "Griptape Bridge",
    "author": "Griptape Community",
    "version": (0, 7, 5),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Griptape",
    "description": "Exchange media with Griptape Nodes and let flows drive the scene",
    "category": "Pipeline",
}

import base64
import json
import math
import os
import queue
import tempfile
import textwrap
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

import bpy
import bpy.utils.previews
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from . import phonecam

# Must match BRIDGE_VERSION in the GTN library's bridge_server.py; a mismatch
# means the engine is running stale bridge code and needs a restart.
EXPECTED_BRIDGE_VERSION = "0.6.6"

NET_POLL_INTERVAL = 1.5
NET_OFFLINE_POLL_INTERVAL = 5.0
MAIN_PUMP_INTERVAL = 0.3
HTTP_TIMEOUT = 2.0
DOWNLOAD_TIMEOUT = 120.0

_last_seen_outbound_version = 0
_consecutive_failures = 0

# Asset browser state (module-level: previews collection + scanned files).
_asset_previews = None
_assets: list = []  # [{"name", "path", "kind", "icon_id", "mtime"}]

# Proxy node runner state (module-level: Blender props can't hold rich data).
_catalog_nodes: list = []
_node_enum_cache: list = []
_choice_enum_cache: dict = {}
_active_job_id: str = ""
_agent_job_id: str = ""

# Keep enum item tuples referenced at module level: Blender's dynamic enum
# callbacks require the strings to stay alive or the UI shows garbage.
_agent_model_items: list = []


def _agent_model_enum_items() -> list:
    items = [("DEFAULT", "Default (engine picks)", "Use the Griptape Cloud default model")]
    items.extend(_agent_model_items)
    return items

# All polling I/O happens on a background thread so the Blender UI never
# blocks on the network. The two queues shuttle work across threads:
#   _to_main: things that must run on Blender's main thread (place media,
#             execute scene commands, update status props).
#   _to_net:  things that must go back over HTTP (command results).
# _flags mirrors scene settings/prefs into plain Python so the network
# thread never touches bpy data.
_to_main: "queue.Queue[tuple]" = queue.Queue()
_to_net: "queue.Queue[tuple]" = queue.Queue()
_flags: dict = {"auto_poll": False, "listen": False, "job_id": "", "host": "127.0.0.1", "port": 7890, "force_pull": False}
_net_stop = threading.Event()
_net_thread = None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class GriptapeBridgePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    host: StringProperty(name="Host", default="127.0.0.1")
    port: IntProperty(name="Port", default=7890, min=1, max=65535)
    allow_python: BoolProperty(
        name="Allow Python Commands",
        description="Let GTN flows/agents execute arbitrary Python in this Blender session (run_python command)",
        default=False,
    )

    def draw(self, _context):
        layout = self.layout
        row = layout.row()
        row.prop(self, "host")
        row.prop(self, "port")
        layout.prop(self, "allow_python")
        if self.allow_python:
            layout.label(text="Python commands can do anything Blender can. Only enable for trusted flows.", icon="ERROR")


def _param_enum_items(self, _context):
    """Dynamic dropdown items for one proxy parameter (choices come from GTN)."""
    key = self.choices_json or "[]"
    cached = _choice_enum_cache.get(key)
    if cached is not None:
        return cached
    try:
        choices = json.loads(key)
    except Exception:
        choices = []
    items = [(str(c), str(c), "") for c in choices] or [("__none__", "-", "")]
    _choice_enum_cache[key] = items
    return items


class GriptapeProxyParam(bpy.types.PropertyGroup):
    # 'name' (inherited) stores the GTN parameter name.
    label: StringProperty(default="")
    tooltip: StringProperty(default="")
    ui_type: StringProperty(default="string")  # string|int|float|bool|enum|media|json
    multiline: BoolProperty(default=False)
    choices_json: StringProperty(default="[]")
    string_value: StringProperty(name="", default="")
    int_value: IntProperty(name="", default=0)
    float_value: FloatProperty(name="", default=0.0)
    bool_value: BoolProperty(name="", default=False)
    enum_value: EnumProperty(name="", items=_param_enum_items)
    attach_source: EnumProperty(
        name="Source",
        description="What to feed into this media input",
        items=[
            ("NONE", "None", "Don't attach anything - the node uses its own default"),
            ("CAMERA", "Camera View", "OpenGL render of the active camera at the capture resolution"),
            ("VIEWPORT", "Viewport", "Capture the viewport exactly as you see it"),
            ("FILE", "File...", "Use an image/video file from disk"),
        ],
        default="NONE",
    )
    file_path: StringProperty(name="File", subtype="FILE_PATH", default="")


_TAB_KEYWORDS = (
    ("VIDEO", ("video", "seedance", "ltx", "film", "movie", "interpolat", "denoise", "remover", "matanyone")),
    ("IMAGE", ("image", "img", "photo", "flux", "upscale", "diffusion", "sdxl", "banana", "seedream", "paint", "tile", "color")),
    ("THREED", ("3d", "mesh", "camera", "blender", "sketchfab", "depth", "pose", "360", "latlong", "sphere", "focal", "ue5")),
    ("TEXT", ("text", "prompt", "agent", "llm", "story", "chat", "director", "planner")),
    ("AUDIO", ("audio", "sound", "music", "voice", "speech")),
)


def _bucket_for(entry: dict) -> str:
    haystack = f"{entry.get('category', '')} {entry.get('display_name', '')} {entry.get('node_type', '')}".lower()
    for tab, words in _TAB_KEYWORDS:
        if any(word in haystack for word in words):
            return tab
    return "OTHER"


def _node_enum_items(self, _context):
    """Dynamic dropdown of GTN node types, filtered by curated list + tab + search field."""
    global _node_enum_cache
    query = (self.proxy_filter or "").lower().strip()
    tab = self.proxy_tab
    curated_only = self.proxy_curated and any(e.get("curated") for e in _catalog_nodes)
    items = []
    for entry in _catalog_nodes:
        if curated_only and not entry.get("curated"):
            continue
        if tab != "ALL" and entry.get("_bucket", "OTHER") != tab:
            continue
        label = entry.get("display_name") or entry.get("node_type", "?")
        haystack = f"{label} {entry.get('node_type', '')} {entry.get('category', '')} {entry.get('library', '')}".lower()
        if query and query not in haystack:
            continue
        identifier = f"{entry.get('library', '')}||{entry.get('node_type', '')}"
        description = (entry.get("description") or entry.get("node_type", ""))[:120]
        items.append((identifier, f"{label}  [{entry.get('library', '')}]", description))
        if len(items) >= 200:
            break
    if not items:
        items = [("__none__", "No nodes - refresh catalog", "")]
    _node_enum_cache = items
    return items


class GriptapeBridgeSettings(bpy.types.PropertyGroup):
    prompt: StringProperty(
        name="Prompt",
        description="Sent to GTN as metadata with every capture",
        default="",
    )
    capture_width: IntProperty(name="Width", default=1280, min=64, max=8192)
    capture_height: IntProperty(name="Height", default=720, min=64, max=8192)
    auto_poll: BoolProperty(
        name="Auto-Receive",
        description="Poll the bridge and place new media from GTN automatically",
        default=True,
    )
    listen_commands: BoolProperty(
        name="Listen for Commands",
        description="Execute scene commands queued by GTN flows / agents",
        default=False,
    )
    place_mode: EnumProperty(
        name="Place As",
        description="How incoming images are placed in the scene",
        items=[
            ("IMAGE_EDITOR", "Image Editor", "Load into an Image Editor space"),
            ("PLANE", "Plane at Cursor", "Create a textured plane at the 3D cursor"),
            ("CAM_BG", "Camera Background", "Set as background image on the active camera"),
            ("SEQUENCER", "Sequencer Strip", "Add as a strip in the Video Sequencer (video/image)"),
            ("SAVE_ONLY", "Save to Disk Only", "Just save into the bridge folder"),
        ],
        default="PLANE",
    )
    status: StringProperty(name="Status", default="Not connected yet.")
    last_received: StringProperty(name="Last Received", default="")
    connected: BoolProperty(name="Connected", default=False)
    signed_in: BoolProperty(name="Signed In", default=False)
    key_hint: StringProperty(default="")

    # Proxy node runner
    proxy_filter: StringProperty(
        name="Search",
        description="Filter the GTN node catalog",
        default="",
        options={"TEXTEDIT_UPDATE"},
    )
    proxy_tab: EnumProperty(
        name="Tab",
        items=[
            ("ALL", "All", "Every registered node"),
            ("IMAGE", "Image", "Image generation and editing nodes"),
            ("VIDEO", "Video", "Video generation and editing nodes"),
            ("THREED", "3D", "3D, camera, and depth nodes"),
            ("TEXT", "Text", "Prompt, agent, and text nodes"),
            ("AUDIO", "Audio", "Audio nodes"),
            ("OTHER", "Misc", "Everything else"),
        ],
        default="ALL",
    )
    proxy_curated: BoolProperty(
        name="Curated Only",
        description="Show only the curated node list (edit curated_nodes.json in the GTN library to add more)",
        default=True,
    )
    proxy_node: EnumProperty(name="Node", items=_node_enum_items)
    proxy_params: CollectionProperty(type=GriptapeProxyParam)
    proxy_loaded_for: StringProperty(default="")
    proxy_job_status: StringProperty(default="")

    # Scene agent
    agent_prompt: StringProperty(
        name="Scene Request",
        description="Describe what to build; the Griptape agent assembles it with scene tools",
        default="",
    )
    agent_model: EnumProperty(
        name="LLM",
        description="Model that powers the scene agent (list comes from Griptape Cloud)",
        items=lambda self, context: _agent_model_enum_items(),
    )
    agent_status: StringProperty(default="")

    # Asset browser
    float_tab: EnumProperty(
        name="Mode",
        items=[
            ("GENERATE", "Generate", "Run a Griptape node"),
            ("AGENT", "Agent", "Describe a scene; a Griptape agent builds it with tools"),
            ("ASSETS", "Assets", "Browse everything received from Griptape"),
        ],
        default="GENERATE",
    )
    asset_kind: EnumProperty(
        name="Kind",
        items=[
            ("ALL", "All", "All assets"),
            ("image", "Image", "Images"),
            ("video", "Video", "Videos"),
            ("model", "3D", "3D models"),
        ],
        default="ALL",
    )


def _set_connected(value: bool) -> None:
    settings = _settings()
    if settings is not None and settings.connected != value:
        settings.connected = value


def _prefs():
    return bpy.context.preferences.addons[__package__].preferences


def _settings(scene=None):
    scene = scene or _active_scene()
    return scene.griptape_bridge if scene else None


def _active_scene():
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        return scene
    return bpy.data.scenes[0] if bpy.data.scenes else None


def _base_url() -> str:
    # Read from the mirrored flags so this is safe on any thread; the main
    # pump keeps them in sync with the add-on preferences.
    return f"http://{_flags.get('host', '127.0.0.1')}:{_flags.get('port', 7890)}"


def _set_status(message: str) -> None:
    settings = _settings()
    if settings is not None:
        settings.status = message[:240]
    print(f"[GriptapeBridge] {message}")


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only; Blender ships no requests)
# ---------------------------------------------------------------------------
def _http_json(path: str, timeout: float = HTTP_TIMEOUT) -> dict:
    req = urllib.request.Request(_base_url() + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(path: str, payload: dict, timeout: float = HTTP_TIMEOUT) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _base_url() + path, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_download(path: str, timeout: float = DOWNLOAD_TIMEOUT) -> bytes:
    req = urllib.request.Request(_base_url() + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _post_media(data: bytes, filename: str, kind: str, content_type: str, meta: dict) -> dict:
    meta_header = base64.b64encode(json.dumps(meta).encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        _base_url() + "/to_gtn",
        data=data,
        method="POST",
        headers={
            "Content-Type": content_type,
            "X-Filename": filename,
            "X-Kind": kind,
            "X-Meta": meta_header,
        },
    )
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Capture + metadata
# ---------------------------------------------------------------------------
def _camera_meta(scene) -> dict:
    cam = scene.camera
    if cam is None or cam.type != "CAMERA":
        return {}
    loc = cam.location
    rot = cam.rotation_euler
    data = cam.data
    return {
        "name": cam.name,
        "location": [float(loc.x), float(loc.y), float(loc.z)],
        "rotation_euler": [float(rot.x), float(rot.y), float(rot.z)],
        "lens_mm": float(getattr(data, "lens", 0.0)),
        "sensor_width_mm": float(getattr(data, "sensor_width", 0.0)),
        "fov_degrees": math.degrees(float(data.angle)) if hasattr(data, "angle") else None,
        "clip_start": float(getattr(data, "clip_start", 0.0)),
        "clip_end": float(getattr(data, "clip_end", 0.0)),
    }


def _shot_meta(scene) -> dict:
    settings = _settings(scene)
    return {
        "source": "blender",
        "prompt": settings.prompt if settings else "",
        "camera": _camera_meta(scene),
        "scene": {
            "name": scene.name,
            "frame_current": int(scene.frame_current),
            "frame_start": int(scene.frame_start),
            "frame_end": int(scene.frame_end),
            "fps": float(scene.render.fps) / float(scene.render.fps_base),
            "resolution": [int(scene.render.resolution_x), int(scene.render.resolution_y)],
            "blend_file": bpy.data.filepath or "",
        },
    }


def _capture_opengl(scene, width: int, height: int, view_context: bool) -> bytes:
    """OpenGL render (viewport or active camera) and return PNG bytes."""
    prev_x = scene.render.resolution_x
    prev_y = scene.render.resolution_y
    prev_pct = scene.render.resolution_percentage
    prev_path = scene.render.filepath
    prev_format = scene.render.image_settings.file_format

    scene.render.resolution_x = int(width)
    scene.render.resolution_y = int(height)
    scene.render.resolution_percentage = 100

    tmp_path = None
    try:
        bpy.ops.render.opengl(animation=False, sequencer=False, view_context=view_context)
        render_result = bpy.data.images.get("Render Result")
        if not render_result:
            raise RuntimeError("No 'Render Result' after capture.")

        with tempfile.NamedTemporaryFile(prefix="gtn_capture_", suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        render_result.save_render(tmp_path)
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        scene.render.resolution_x = prev_x
        scene.render.resolution_y = prev_y
        scene.render.resolution_percentage = prev_pct
        scene.render.filepath = prev_path
        scene.render.image_settings.file_format = prev_format


# ---------------------------------------------------------------------------
# Receiving + placement
# ---------------------------------------------------------------------------
def _bridge_dir() -> str:
    if bpy.data.filepath:
        folder = bpy.path.abspath("//griptape_bridge")
    else:
        folder = os.path.join(tempfile.gettempdir(), "griptape_bridge")
    os.makedirs(folder, exist_ok=True)
    return folder


def _save_received(data: bytes, filename: str, version: int) -> str:
    stem, ext = os.path.splitext(filename or "gtn_result.png")
    safe = "".join(c for c in stem if c.isalnum() or c in ("-", "_")) or "gtn_result"
    path = os.path.join(_bridge_dir(), f"{safe}_v{version}{ext or '.png'}")
    with open(path, "wb") as f:
        f.write(data)
    return path


# ---------------------------------------------------------------------------
# Asset browser: every file the bridge ever saved, as a thumbnail grid.
# ---------------------------------------------------------------------------
_ASSET_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_ASSET_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")
_ASSET_MODEL_EXTS = (".glb", ".gltf", ".obj", ".fbx", ".usd", ".usdc", ".usda", ".usdz", ".stl", ".ply", ".bvh")


def _asset_kind_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _ASSET_IMAGE_EXTS:
        return "image"
    if ext in _ASSET_VIDEO_EXTS:
        return "video"
    if ext in _ASSET_MODEL_EXTS:
        return "model"
    return ""


def _scan_assets() -> None:
    """Rescan the bridge folder and (lazily) build thumbnails. Main thread only."""
    global _asset_previews, _assets
    if _asset_previews is None:
        _asset_previews = bpy.utils.previews.new()

    folder = _bridge_dir()
    found = []
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    def _load_preview(path: str, kind: str) -> int:
        key = path.replace("\\", "/")
        preview = _asset_previews.get(key)
        if preview is None:
            try:
                preview = _asset_previews.load(key, path, "MOVIE" if kind == "video" else "IMAGE")
            except Exception:
                return 0
        return preview.icon_id

    for name in names:
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or ".thumb." in name:
            continue
        kind = _asset_kind_for(path)
        if not kind:
            continue
        icon_id = 0
        if kind in ("image", "video"):
            icon_id = _load_preview(path, kind)
        elif kind == "model":
            # Providers often render a textured preview; the bridge saves it
            # as a "<model>.thumb.<ext>" sidecar next to the model file.
            for thumb_ext in (".thumb.webp", ".thumb.png", ".thumb.jpg", ".thumb.jpeg"):
                thumb_path = path + thumb_ext
                if os.path.isfile(thumb_path):
                    icon_id = _load_preview(thumb_path, "image")
                    break
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        found.append({"name": name, "path": path, "kind": kind, "icon_id": icon_id, "mtime": mtime})

    found.sort(key=lambda a: a["mtime"], reverse=True)
    _assets = found


def _place_image_in_editor(image) -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "IMAGE_EDITOR":
                area.spaces.active.image = image
                return
    # No image editor open; the image is still loaded in bpy.data.images.


def _make_image_plane(image, location=None) -> None:
    scene = _active_scene()
    aspect = image.size[0] / image.size[1] if image.size[1] else 1.0
    mesh = bpy.data.meshes.new(f"GTN_{image.name}")
    half_w, half_h = aspect / 2.0, 0.5
    mesh.from_pydata(
        [(-half_w, -half_h, 0), (half_w, -half_h, 0), (half_w, half_h, 0), (-half_w, half_h, 0)],
        [],
        [(0, 1, 2, 3)],
    )
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for i, uv in enumerate([(0, 0), (1, 0), (1, 1), (0, 1)]):
        uv_layer.data[i].uv = uv
    mesh.update()

    material = bpy.data.materials.new(name=f"GTN_{image.name}")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    if image.source == "MOVIE":
        tex.image_user.frame_duration = image.frame_duration
        tex.image_user.frame_start = _active_scene().frame_start
        tex.image_user.use_auto_refresh = True
        tex.image_user.use_cyclic = True
    emission = nodes.new("ShaderNodeEmission")
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(tex.outputs["Color"], emission.inputs["Color"])
    links.new(emission.outputs["Emission"], output.inputs["Surface"])

    obj = bpy.data.objects.new(f"GTN_{image.name}", mesh)
    obj.data.materials.append(material)
    obj.location = location if location is not None else scene.cursor.location
    scene.collection.objects.link(obj)


def _set_camera_background(image) -> None:
    scene = _active_scene()
    cam = scene.camera
    if cam is None or cam.type != "CAMERA":
        raise RuntimeError("No active camera for background image.")
    cam.data.show_background_images = True
    bg = cam.data.background_images.new()
    bg.image = image
    bg.alpha = 1.0


def _add_sequencer_strip(path: str, kind: str) -> None:
    scene = _active_scene()
    if scene.sequence_editor is None:
        scene.sequence_editor_create()
    editor = scene.sequence_editor
    occupied = {strip.channel for strip in editor.sequences_all}
    channel = 1
    while channel in occupied:
        channel += 1
    name = os.path.basename(path)
    frame = int(scene.frame_current)
    if kind == "video":
        editor.sequences.new_movie(name=name, filepath=path, channel=channel, frame_start=frame)
    else:
        editor.sequences.new_image(name=name, filepath=path, channel=channel, frame_start=frame)


def _import_model(path: str) -> str:
    """Import a 3D file into the scene at the cursor. Returns a status message."""
    ext = os.path.splitext(path)[1].lower()
    before = set(bpy.data.objects)
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext in (".usd", ".usdc", ".usda", ".usdz"):
        bpy.ops.wm.usd_import(filepath=path)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    elif ext == ".bvh":
        # Mocap: imports as an animated armature scaled to scene FPS.
        bpy.ops.import_anim.bvh(filepath=path, use_fps_scale=True)
    else:
        return f"Model saved (no importer for {ext}): {path}"

    new_objects = [obj for obj in bpy.data.objects if obj not in before]
    cursor = _active_scene().cursor.location
    roots = [obj for obj in new_objects if obj.parent is None]
    for obj in roots:
        obj.location = (obj.location[0] + cursor[0], obj.location[1] + cursor[1], obj.location[2] + cursor[2])
    return f"Model imported ({len(new_objects)} objects): {os.path.basename(path)}"


def _place_received(path: str, kind: str, place_mode: str) -> str:
    if kind == "model":
        return _import_model(path)

    if kind == "video":
        if place_mode == "SEQUENCER":
            _add_sequencer_strip(path, "video")
            return f"Video added to sequencer: {os.path.basename(path)}"
        if place_mode == "PLANE":
            movie = bpy.data.images.load(path)
            _make_image_plane(movie)
            return f"Video placed as plane (animated texture): {os.path.basename(path)}"
        return f"Video saved: {path}"

    image = bpy.data.images.load(path)
    if place_mode == "PLANE":
        _make_image_plane(image)
        return f"Image placed as plane: {image.name}"
    if place_mode == "CAM_BG":
        _set_camera_background(image)
        return f"Image set as camera background: {image.name}"
    if place_mode == "SEQUENCER":
        _add_sequencer_strip(path, "image")
        return f"Image added to sequencer: {image.name}"
    if place_mode == "SAVE_ONLY":
        return f"Image saved: {path}"
    _place_image_in_editor(image)
    return f"Image loaded in editor: {image.name}"


def _apply_received(meta: dict, data: bytes) -> None:
    """Save + place downloaded media. Must run on the main thread."""
    version = int(meta.get("version", 0) or 0)
    kind = str(meta.get("kind", "image"))
    default_name = {"video": "gtn_result.mp4", "model": "gtn_result.glb"}.get(kind, "gtn_result.png")
    filename = str(meta.get("filename", "") or default_name)
    path = _save_received(data, filename, version)

    # Textured preview shipped alongside 3D models -> thumbnail sidecar.
    inner_meta = meta.get("meta") or {}
    thumb_b64 = inner_meta.get("thumbB64")
    if thumb_b64:
        try:
            ext = str(inner_meta.get("thumbExt", ".webp") or ".webp")
            with open(path + ".thumb" + ext, "wb") as f:
                f.write(base64.b64decode(thumb_b64))
        except Exception:
            pass

    settings = _settings()
    place_mode = settings.place_mode if settings else "SAVE_ONLY"
    message = _place_received(path, kind, place_mode)
    if settings is not None:
        settings.last_received = os.path.basename(path)
    try:
        _scan_assets()
    except Exception:
        pass
    _set_status(message)


def _pull_latest(force: bool = False) -> bool:
    """Blocking check+place (used by the explicit 'Get Latest' button)."""
    global _last_seen_outbound_version
    meta = _http_json("/to_blender/latest")
    version = int(meta.get("version", 0) or 0)
    if not meta.get("hasMedia"):
        if force:
            _set_status("No media from GTN yet.")
        return False
    if not force and version <= _last_seen_outbound_version:
        return False

    data = _http_download("/to_blender/latest/raw")
    _last_seen_outbound_version = version
    _apply_received(meta, data)
    return True


# ---------------------------------------------------------------------------
# Command executor (runs on the main thread from the poll timer)
# ---------------------------------------------------------------------------
def _vec3(value, fallback):
    if isinstance(value, dict):
        return (
            float(value.get("x", fallback[0])),
            float(value.get("y", fallback[1])),
            float(value.get("z", fallback[2])),
        )
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (float(value[0]), float(value[1]), float(value[2]))
    return tuple(fallback)


def _resolve_camera(name):
    if name:
        obj = bpy.data.objects.get(name)
        if obj and obj.type == "CAMERA":
            return obj
    scene = _active_scene()
    if scene.camera and scene.camera.type == "CAMERA":
        return scene.camera
    for obj in bpy.data.objects:
        if obj.type == "CAMERA":
            return obj
    raise RuntimeError("No camera found in the current file.")


def _camera_payload(cam) -> dict:
    return {
        "name": cam.name,
        "location": [float(v) for v in cam.location],
        "rotation_euler": [float(v) for v in cam.rotation_euler],
        "lens_mm": float(getattr(cam.data, "lens", 0.0)),
    }


def _cmd_scene_info(_params: dict) -> dict:
    scene = _active_scene()
    return {
        "scene": scene.name,
        "frame_current": int(scene.frame_current),
        "frame_start": int(scene.frame_start),
        "frame_end": int(scene.frame_end),
        "fps": float(scene.render.fps) / float(scene.render.fps_base),
        "resolution": [int(scene.render.resolution_x), int(scene.render.resolution_y)],
        "active_camera": scene.camera.name if scene.camera else None,
        "objects": [
            {"name": obj.name, "type": obj.type, "location": [float(v) for v in obj.location]}
            for obj in scene.objects
        ],
    }


def _cmd_list_cameras(_params: dict) -> dict:
    return {"cameras": [obj.name for obj in bpy.data.objects if obj.type == "CAMERA"]}


def _cmd_get_camera(params: dict) -> dict:
    return {"camera": _camera_payload(_resolve_camera(params.get("name")))}


def _cmd_set_camera(params: dict) -> dict:
    cam = _resolve_camera(params.get("name"))
    if "location" in params:
        cam.location = _vec3(params["location"], cam.location)
    if "rotation_euler" in params:
        cam.rotation_euler = _vec3(params["rotation_euler"], cam.rotation_euler)
    if "lens_mm" in params:
        cam.data.lens = float(params["lens_mm"])
    if params.get("keyframe"):
        frame = int(params.get("frame", _active_scene().frame_current))
        cam.keyframe_insert(data_path="location", frame=frame)
        cam.keyframe_insert(data_path="rotation_euler", frame=frame)
    bpy.context.view_layer.update()
    return {"camera": _camera_payload(cam)}


def _cmd_snapshot(params: dict) -> dict:
    scene = _active_scene()
    width = int(params.get("width", 1280))
    height = int(params.get("height", 720))
    prev_camera = scene.camera
    camera_name = params.get("camera")
    try:
        if camera_name:
            scene.camera = _resolve_camera(camera_name)
        data = _capture_opengl(scene, width, height, view_context=False)
    finally:
        scene.camera = prev_camera
    return {
        "camera": (camera_name or (prev_camera.name if prev_camera else "")),
        "width": width,
        "height": height,
        "image_data_url": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
    }


_PRIMITIVE_OPS = {
    "cube": lambda: bpy.ops.mesh.primitive_cube_add(),
    "sphere": lambda: bpy.ops.mesh.primitive_uv_sphere_add(),
    "plane": lambda: bpy.ops.mesh.primitive_plane_add(),
    "cylinder": lambda: bpy.ops.mesh.primitive_cylinder_add(),
    "cone": lambda: bpy.ops.mesh.primitive_cone_add(),
    "torus": lambda: bpy.ops.mesh.primitive_torus_add(),
    "empty": lambda: bpy.ops.object.empty_add(),
    "camera": lambda: bpy.ops.object.camera_add(),
    "monkey": lambda: bpy.ops.mesh.primitive_monkey_add(),
    "light": lambda: bpy.ops.object.light_add(type="POINT"),
    "sun": lambda: bpy.ops.object.light_add(type="SUN"),
    "area_light": lambda: bpy.ops.object.light_add(type="AREA"),
    # Aliases matching the scene agent's tool schema.
    "light_point": lambda: bpy.ops.object.light_add(type="POINT"),
    "light_sun": lambda: bpy.ops.object.light_add(type="SUN"),
    "light_area": lambda: bpy.ops.object.light_add(type="AREA"),
    "light_spot": lambda: bpy.ops.object.light_add(type="SPOT"),
}


def _cmd_add_primitive(params: dict) -> dict:
    kind = str(params.get("kind", "cube")).lower()
    op = _PRIMITIVE_OPS.get(kind)
    if op is None:
        raise RuntimeError(f"Unknown primitive '{kind}'. Options: {sorted(_PRIMITIVE_OPS)}")
    op()
    obj = bpy.context.active_object
    if obj is None:
        raise RuntimeError("Primitive was not created.")
    if params.get("name"):
        obj.name = str(params["name"])
    if "location" in params:
        obj.location = _vec3(params["location"], obj.location)
    if "rotation_euler" in params:
        obj.rotation_euler = _vec3(params["rotation_euler"], obj.rotation_euler)
    if "scale" in params:
        obj.scale = _vec3(params["scale"], obj.scale)
    return {"object": obj.name, "location": [float(v) for v in obj.location]}


def _cmd_move_object(params: dict) -> dict:
    name = str(params.get("name", ""))
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise RuntimeError(f"Object '{name}' not found.")
    if "location" in params:
        obj.location = _vec3(params["location"], obj.location)
    if "rotation_euler" in params:
        obj.rotation_euler = _vec3(params["rotation_euler"], obj.rotation_euler)
    if "scale" in params:
        obj.scale = _vec3(params["scale"], obj.scale)
    bpy.context.view_layer.update()
    return {"object": obj.name, "location": [float(v) for v in obj.location]}


def _cmd_delete_object(params: dict) -> dict:
    name = str(params.get("name", ""))
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise RuntimeError(f"Object '{name}' not found.")
    bpy.data.objects.remove(obj, do_unlink=True)
    return {"deleted": name}


def _cmd_import_latest_as_plane(params: dict) -> dict:
    meta = _http_json("/to_blender/latest")
    if not meta.get("hasMedia") or meta.get("kind") != "image":
        raise RuntimeError("No image staged on the bridge (send one from GTN first).")
    data = _http_download("/to_blender/latest/raw")
    version = int(meta.get("version", 0) or 0)
    path = _save_received(data, str(meta.get("filename", "gtn_result.png")), version)
    image = bpy.data.images.load(path)
    location = _vec3(params["location"], _active_scene().cursor.location) if "location" in params else None
    _make_image_plane(image, location=location)
    return {"image": image.name, "path": path}


def _cmd_set_frame(params: dict) -> dict:
    scene = _active_scene()
    scene.frame_set(int(params.get("frame", scene.frame_current)))
    return {"frame_current": int(scene.frame_current)}


def _cmd_run_python(params: dict) -> dict:
    if not _prefs().allow_python:
        raise RuntimeError("Python commands are disabled. Enable 'Allow Python Commands' in the add-on preferences.")
    code = str(params.get("code", ""))
    if not code.strip():
        raise RuntimeError("Empty 'code' parameter.")
    namespace: dict = {"bpy": bpy}
    exec(code, namespace)  # noqa: S102 - explicit opt-in via preferences
    result = namespace.get("result")
    try:
        json.dumps(result)
        return {"result": result}
    except (TypeError, ValueError):
        return {"result": repr(result)}


_COMMANDS = {
    "scene_info": _cmd_scene_info,
    "list_cameras": _cmd_list_cameras,
    "get_camera": _cmd_get_camera,
    "set_camera": _cmd_set_camera,
    "snapshot": _cmd_snapshot,
    "add_primitive": _cmd_add_primitive,
    "move_object": _cmd_move_object,
    "delete_object": _cmd_delete_object,
    "import_latest_as_plane": _cmd_import_latest_as_plane,
    "set_frame": _cmd_set_frame,
    "run_python": _cmd_run_python,
}


def _execute_command_entry(entry: dict) -> None:
    """Execute a scene command on the main thread; queue the result for HTTP."""
    command_id = entry.get("id", "")
    name = str(entry.get("command", ""))
    params = entry.get("params") or {}
    handler = _COMMANDS.get(name)

    if handler is None:
        result = {"error": f"Unknown command '{name}'. Available: {sorted(_COMMANDS)}"}
        ok = False
    else:
        try:
            result = handler(params if isinstance(params, dict) else {})
            ok = True
        except Exception as exc:
            result = {"error": str(exc), "traceback": traceback.format_exc()}
            ok = False

    _to_net.put(("command_result", {"id": command_id, "ok": ok, "result": result}))
    _set_status(f"Command '{name}': {'ok' if ok else result.get('error', 'failed')}")


# ---------------------------------------------------------------------------
# Background network thread: all polling I/O lives here, never on the UI.
# ---------------------------------------------------------------------------
_auth_checked = False


def _net_loop() -> None:
    global _last_seen_outbound_version, _consecutive_failures, _auth_checked
    while not _net_stop.is_set():
        # Always flush outgoing command results, even when polling is off.
        while True:
            try:
                kind, payload = _to_net.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "command_result":
                    _http_post_json("/commands/result", payload)
            except Exception:
                pass

        active = (
            _flags.get("auto_poll")
            or _flags.get("listen")
            or _flags.get("job_id")
            or _flags.get("agent_job_id")
            or _flags.get("force_pull")
        )
        if active:
            try:
                if _flags.get("auto_poll") or _flags.get("force_pull"):
                    force = bool(_flags.get("force_pull"))
                    meta = _http_json("/to_blender/latest")
                    version = int(meta.get("version", 0) or 0)
                    if meta.get("hasMedia") and (force or version > _last_seen_outbound_version):
                        data = _http_download("/to_blender/latest/raw")
                        _last_seen_outbound_version = version
                        _to_main.put(("place", meta, data))
                    _flags["force_pull"] = False

                if _flags.get("listen"):
                    for _ in range(5):
                        payload = _http_json("/commands/next")
                        entry = payload.get("command")
                        if not entry:
                            break
                        _to_main.put(("command", entry, None))

                job_id = _flags.get("job_id")
                if job_id:
                    payload = _http_json(f"/proxy/jobs/{job_id}")
                    _to_main.put(("job", payload, None))

                agent_job_id = _flags.get("agent_job_id")
                if agent_job_id:
                    payload = _http_json(f"/agent/jobs/{agent_job_id}")
                    _to_main.put(("agent_job", payload, None))

                if not _auth_checked:
                    try:
                        health = _http_json("/health")
                        server_version = str(health.get("version", "") or "")
                        if server_version != EXPECTED_BRIDGE_VERSION:
                            _to_main.put(
                                (
                                    "status",
                                    f"Engine bridge is outdated ({server_version or 'pre-0.4'}) - "
                                    "RESTART the GTN engine to update it.",
                                    None,
                                )
                            )
                    except Exception:
                        pass
                    try:
                        auth = _http_json("/auth/status")
                        if auth.get("ok"):
                            _to_main.put(("auth", auth, None))
                    except Exception:
                        pass
                    try:
                        models = _http_json("/agent/models")
                        if models.get("ok"):
                            _to_main.put(("agent_models", models, None))
                    except Exception:
                        pass
                    _auth_checked = True

                _consecutive_failures = 0
                _to_main.put(("online", True, None))
            except (urllib.error.URLError, OSError, TimeoutError):
                _consecutive_failures += 1
                _auth_checked = False
                _to_main.put(("online", False, None))
            except Exception as exc:
                _consecutive_failures += 1
                _to_main.put(("status", f"Bridge error: {exc}", None))

        interval = NET_OFFLINE_POLL_INTERVAL if _consecutive_failures else NET_POLL_INTERVAL
        if _net_stop.wait(interval):
            break


# ---------------------------------------------------------------------------
# Main-thread pump: cheap queue draining, no network calls.
# ---------------------------------------------------------------------------
def _handle_job_update(settings, payload: dict) -> None:
    global _active_job_id
    status = payload.get("status", "")
    if status == "done":
        _active_job_id = ""
        result = payload.get("result") or {}
        staged = result.get("staged_media")
        if staged:
            kind = staged.get("kind", "media")
            settings.proxy_job_status = f"Done - {kind} received."
            _set_status("Generation finished.")
            _flags["force_pull"] = True  # net thread fetches the media next tick
        else:
            # Text nodes (e.g. Agent): surface the text instead of "no media".
            outputs = result.get("outputs") or {}
            texts = [v for v in outputs.values() if isinstance(v, str) and v.strip()]
            if texts:
                text = max(texts, key=len)
                try:
                    bpy.context.window_manager.clipboard = text
                    settings.proxy_job_status = f"Done: {text}"[:200]
                    _set_status("Text result copied to clipboard.")
                except Exception:
                    settings.proxy_job_status = f"Done: {text}"[:200]
            else:
                settings.proxy_job_status = "Done - no media output detected."
                _set_status("Generation finished (no importable media in outputs).")
    elif status == "error":
        _active_job_id = ""
        error = payload.get("error", "unknown error")
        settings.proxy_job_status = f"Failed: {error}"[:200]
        _set_status(f"Generation failed: {error}")
    else:
        settings.proxy_job_status = f"{status}..."


def _handle_agent_job_update(settings, payload: dict) -> None:
    global _agent_job_id
    status = payload.get("status", "")
    actions = payload.get("actions") or []
    if status == "done":
        _agent_job_id = ""
        text = str(payload.get("result") or "").strip()
        settings.agent_status = f"Done ({len(actions)} actions). {text}"[:400]
        try:
            bpy.context.window_manager.clipboard = text
        except Exception:
            pass
        _set_status("Scene agent finished - summary copied to clipboard.")
    elif status == "cancelled":
        _agent_job_id = ""
        settings.agent_status = f"Cancelled ({len(actions)} actions done)."
        _set_status("Scene agent cancelled.")
    elif status == "error":
        _agent_job_id = ""
        error = str(payload.get("error", "unknown error")).splitlines()[0]
        settings.agent_status = f"Failed: {error}"[:400]
        _set_status(f"Scene agent failed: {error}")
    else:
        last = actions[-1]["command"] if actions else "thinking"
        settings.agent_status = f"Working... {len(actions)} actions (last: {last})"


def _redraw_sidebars() -> None:
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass


def _main_pump():
    settings = _settings()
    if settings is None:
        return MAIN_PUMP_INTERVAL

    # Mirror settings/prefs into plain flags for the network thread.
    try:
        prefs = _prefs()
        _flags["host"] = prefs.host
        _flags["port"] = int(prefs.port)
    except Exception:
        pass
    _flags["auto_poll"] = bool(settings.auto_poll)
    _flags["listen"] = bool(settings.listen_commands)
    _flags["job_id"] = _active_job_id
    _flags["agent_job_id"] = _agent_job_id

    changed = False
    for _ in range(10):
        try:
            kind, a, b = _to_main.get_nowait()
        except queue.Empty:
            break
        changed = True
        try:
            if kind == "place":
                _apply_received(a, b)
            elif kind == "command":
                _execute_command_entry(a)
            elif kind == "job":
                _handle_job_update(settings, a)
            elif kind == "agent_job":
                _handle_agent_job_update(settings, a)
            elif kind == "agent_models":
                _agent_model_items.clear()
                for name in a.get("models") or []:
                    name = str(name)
                    _agent_model_items.append((name, name, f"Griptape Cloud model '{name}'"))
            elif kind == "auth":
                settings.signed_in = bool(a.get("signed_in"))
                settings.key_hint = str(a.get("key_hint", "") or "")
            elif kind == "online":
                _set_connected(bool(a))
                if not a and settings.status and "offline" not in settings.status.lower():
                    _set_status("Bridge offline - is the GTN engine running with the Blender Bridge library?")
            elif kind == "status":
                _set_status(str(a))
        except Exception as exc:
            _set_status(f"Bridge error: {exc}")

    if changed:
        _redraw_sidebars()
    return MAIN_PUMP_INTERVAL


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------
class GTB_OT_test_connection(bpy.types.Operator):
    bl_idname = "griptape_bridge.test_connection"
    bl_label = "Test Connection"
    bl_description = "Check the GTN bridge server"

    def execute(self, _context):
        try:
            payload = _http_json("/health")
            if payload.get("ok"):
                _set_connected(True)
                try:
                    auth = _http_json("/auth/status")
                    settings = _settings(_context.scene)
                    settings.signed_in = bool(auth.get("signed_in"))
                    settings.key_hint = str(auth.get("key_hint", "") or "")
                except Exception:
                    pass
                server_version = str(payload.get("version", "") or "")
                if server_version != EXPECTED_BRIDGE_VERSION:
                    _set_status(
                        f"Engine bridge is outdated ({server_version or 'pre-0.4'}) - "
                        "RESTART the GTN engine to update it."
                    )
                    self.report({"WARNING"}, "Engine bridge outdated - restart the GTN engine")
                else:
                    _set_status(f"Connected to GTN bridge at {_base_url()}")
                    self.report({"INFO"}, "GTN bridge online")
            else:
                _set_connected(False)
                _set_status(f"Unexpected response from {_base_url()}")
                self.report({"WARNING"}, "Unexpected bridge response")
        except Exception as exc:
            _set_connected(False)
            _set_status(f"Cannot reach GTN bridge at {_base_url()}: {exc}")
            self.report({"ERROR"}, "Bridge offline - start the GTN engine with the Blender Bridge library")
            return {"CANCELLED"}
        return {"FINISHED"}


class GTB_OT_open_griptape(bpy.types.Operator):
    bl_idname = "griptape_bridge.open_griptape"
    bl_label = "Open Griptape"
    bl_description = "Open the Griptape Nodes web editor in your browser (sign in / manage flows there)"

    def execute(self, _context):
        bpy.ops.wm.url_open(url="https://nodes.griptape.ai")
        return {"FINISHED"}


class GTB_OT_sign_in(bpy.types.Operator):
    bl_idname = "griptape_bridge.sign_in"
    bl_label = "Sign in to Griptape"
    bl_description = "Paste your Griptape Cloud API key so generation nodes can run - no need to open the web editor"

    api_key: StringProperty(name="API Key", subtype="PASSWORD", options={"SKIP_SAVE"})

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, _context):
        col = self.layout.column()
        col.label(text="Get a key at nodes.griptape.ai (sign in, then Generate API Key):")
        col.operator("griptape_bridge.open_griptape", text="Open nodes.griptape.ai", icon="URL")
        col.separator()
        col.label(text="Paste the key here:")
        field = col.column()
        field.scale_y = 1.3
        field.prop(self, "api_key", text="")

    def execute(self, context):
        key = self.api_key.strip()
        if not key:
            self.report({"ERROR"}, "No API key entered")
            return {"CANCELLED"}
        try:
            result = _http_post_json("/auth/login", {"api_key": key}, timeout=10.0)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "unknown error"))
        except Exception as exc:
            self.report({"ERROR"}, f"Sign-in failed: {exc}")
            _set_status(f"Sign-in failed: {exc}")
            return {"CANCELLED"}
        settings = _settings(context.scene)
        settings.signed_in = True
        settings.key_hint = f"...{key[-4:]}" if len(key) >= 8 else ""
        _set_status("Signed in to Griptape Cloud.")
        self.report({"INFO"}, "Signed in - generation nodes are ready")
        return {"FINISHED"}


class _SendCaptureBase(bpy.types.Operator):
    view_context = True

    def execute(self, context):
        scene = context.scene
        settings = _settings(scene)
        try:
            data = _capture_opengl(scene, settings.capture_width, settings.capture_height, self.view_context)
            label = "viewport" if self.view_context else "camera"
            result = _post_media(
                data,
                filename=f"blender_{label}_{scene.frame_current:04d}.png",
                kind="image",
                content_type="image/png",
                meta=_shot_meta(scene),
            )
            _set_status(f"Sent {label} capture to GTN (v{result.get('version', '?')}).")
        except Exception as exc:
            _set_status(f"Send failed: {exc}")
            self.report({"ERROR"}, f"Send failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GTB_OT_send_viewport(_SendCaptureBase):
    bl_idname = "griptape_bridge.send_viewport"
    bl_label = "Send Viewport"
    bl_description = "Capture the current viewport (as you see it) and send it to GTN with prompt + camera metadata"
    view_context = True


class GTB_OT_send_camera_view(_SendCaptureBase):
    bl_idname = "griptape_bridge.send_camera_view"
    bl_label = "Send Camera View"
    bl_description = "OpenGL-render the active camera at the set resolution and send it to GTN"
    view_context = False


class _SendFileBase(bpy.types.Operator):
    filepath: StringProperty(subtype="FILE_PATH")
    kind = "image"
    filter_glob_default = "*.png;*.jpg;*.jpeg;*.webp"

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        path = bpy.path.abspath(self.filepath)
        if not os.path.isfile(path):
            self.report({"ERROR"}, "File not found")
            return {"CANCELLED"}
        ext = os.path.splitext(path)[1].lower()
        content_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
        }
        try:
            with open(path, "rb") as f:
                data = f.read()
            result = _post_media(
                data,
                filename=os.path.basename(path),
                kind=self.kind,
                content_type=content_types.get(ext, "application/octet-stream"),
                meta=_shot_meta(context.scene),
            )
            _set_status(f"Sent '{os.path.basename(path)}' to GTN (v{result.get('version', '?')}).")
        except Exception as exc:
            _set_status(f"Send failed: {exc}")
            self.report({"ERROR"}, f"Send failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GTB_OT_send_image_file(_SendFileBase):
    bl_idname = "griptape_bridge.send_image_file"
    bl_label = "Send Image File..."
    bl_description = "Pick an image file on disk and send it to GTN"
    kind = "image"
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.webp", options={"HIDDEN"})


class GTB_OT_send_video_file(_SendFileBase):
    bl_idname = "griptape_bridge.send_video_file"
    bl_label = "Send Video File..."
    bl_description = "Pick a video file on disk (e.g. a rendered animation) and send it to GTN"
    kind = "video"
    filter_glob: StringProperty(default="*.mp4;*.mov;*.webm;*.mkv;*.avi", options={"HIDDEN"})


class GTB_OT_send_animation(bpy.types.Operator):
    bl_idname = "griptape_bridge.send_animation"
    bl_label = "Render & Send Animation"
    bl_description = (
        "OpenGL-render the timeline through the active camera to an MP4 and send it to Griptape "
        "(fast viewport quality; for final quality render normally and use 'Send Video File')"
    )

    def execute(self, context):
        scene = context.scene
        settings = _settings(scene)
        if scene.camera is None:
            self.report({"ERROR"}, "No active camera in the scene")
            return {"CANCELLED"}

        render = scene.render
        img = render.image_settings
        has_media_type = hasattr(img, "media_type")  # Blender 5.0+
        prev = {
            "filepath": render.filepath,
            "file_format": img.file_format,
            "media_type": img.media_type if has_media_type else None,
            "percentage": render.resolution_percentage,
        }
        out_dir = os.path.join(tempfile.gettempdir(), "gtn_playblast")
        os.makedirs(out_dir, exist_ok=True)
        for old in os.listdir(out_dir):
            try:
                os.remove(os.path.join(out_dir, old))
            except OSError:
                pass

        try:
            render.filepath = os.path.join(out_dir, "gtn_animation")
            if has_media_type:
                img.media_type = "VIDEO"  # Blender 5.0: required before FFMPEG is selectable
            img.file_format = "FFMPEG"
            render.ffmpeg.format = "MPEG4"
            render.ffmpeg.codec = "H264"
            render.ffmpeg.constant_rate_factor = "HIGH"
            render.ffmpeg.audio_codec = "NONE"

            started = time.monotonic()
            bpy.ops.render.opengl(animation=True, view_context=False)
            elapsed = time.monotonic() - started

            produced = [os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.lower().endswith(".mp4")]
            if not produced:
                raise RuntimeError("Render finished but no MP4 was written.")
            path = max(produced, key=os.path.getmtime)
            with open(path, "rb") as f:
                data = f.read()

            meta = {
                "prompt": settings.prompt,
                "camera": _camera_meta(scene),
                "source": "animation_playblast",
                "frame_start": int(scene.frame_start),
                "frame_end": int(scene.frame_end),
                "fps": float(scene.render.fps) / float(scene.render.fps_base),
            }
            _post_media(data, "blender_animation.mp4", "video", "video/mp4", meta)
            frames = scene.frame_end - scene.frame_start + 1
            _set_status(f"Animation sent ({frames} frames, {len(data) // 1024} KB).")
            self.report({"INFO"}, f"Sent {frames} frames in {elapsed:.0f}s - load it in GTN's Blender Bridge node")
        except Exception as exc:
            self.report({"ERROR"}, f"Render & send failed: {exc}")
            return {"CANCELLED"}
        finally:
            render.filepath = prev["filepath"]
            if has_media_type:
                img.media_type = prev["media_type"]
            img.file_format = prev["file_format"]
            render.resolution_percentage = prev["percentage"]
        return {"FINISHED"}


def _draw_prompt_text(layout, text: str, min_lines: int = 2) -> None:
    """Read-only wrapped view of a prompt so the whole thing is visible.

    Blender string fields are single-line, so long prompts get clipped; this
    draws the full text as wrapped labels that grow with the content.
    """
    try:
        region_width = bpy.context.region.width
        ui_scale = bpy.context.preferences.system.ui_scale or 1.0
        chars = max(24, int(region_width / (7.2 * ui_scale)))
    except Exception:
        chars = 46
    lines: list = []
    for para in (text or "").splitlines() or [""]:
        lines.extend(textwrap.wrap(para, width=chars) or [""])
    while len(lines) < min_lines:
        lines.append("")
    col = layout.box().column(align=True)
    col.scale_y = 0.8
    for line in lines:
        col.label(text=line)


class GTB_OT_edit_agent_prompt(bpy.types.Operator):
    bl_idname = "griptape_bridge.edit_agent_prompt"
    bl_label = "Edit Scene Prompt"
    bl_description = "Open a wide dialog to write a longer scene description"

    text: StringProperty(name="Scene Request", default="")

    def invoke(self, context, _event):
        self.text = _settings(context.scene).agent_prompt
        return context.window_manager.invoke_props_dialog(self, width=700)

    def draw(self, _context):
        col = self.layout.column()
        col.scale_y = 1.4
        col.prop(self, "text", text="")

    def execute(self, context):
        _settings(context.scene).agent_prompt = self.text
        return {"FINISHED"}


class GTB_OT_edit_prompt(bpy.types.Operator):
    bl_idname = "griptape_bridge.edit_prompt"
    bl_label = "Edit Prompt"
    bl_description = "Open a wide dialog to write a longer prompt"

    param_index: IntProperty(options={"HIDDEN"})
    text: StringProperty(name="Prompt", default="")

    def invoke(self, context, _event):
        settings = _settings(context.scene)
        if self.param_index >= len(settings.proxy_params):
            return {"CANCELLED"}
        self.text = settings.proxy_params[self.param_index].string_value
        return context.window_manager.invoke_props_dialog(self, width=700)

    def draw(self, _context):
        col = self.layout.column()
        col.scale_y = 1.4
        col.prop(self, "text", text="")

    def execute(self, context):
        settings = _settings(context.scene)
        if self.param_index < len(settings.proxy_params):
            settings.proxy_params[self.param_index].string_value = self.text
        return {"FINISHED"}


class GTB_OT_refresh_assets(bpy.types.Operator):
    bl_idname = "griptape_bridge.refresh_assets"
    bl_label = "Refresh Assets"
    bl_description = "Rescan the bridge folder for received media"

    def execute(self, _context):
        _scan_assets()
        _set_status(f"Assets: {len(_assets)} files.")
        return {"FINISHED"}


class GTB_OT_open_asset_folder(bpy.types.Operator):
    bl_idname = "griptape_bridge.open_asset_folder"
    bl_label = "Open Asset Folder"
    bl_description = "Open the bridge media folder in the file browser"

    def execute(self, _context):
        bpy.ops.wm.path_open(filepath=_bridge_dir())
        return {"FINISHED"}


class GTB_OT_place_asset(bpy.types.Operator):
    bl_idname = "griptape_bridge.place_asset"
    bl_label = "Place Asset"
    bl_description = "Place this asset in the scene (models import, images/videos use the Place As mode)"

    filepath: StringProperty(options={"HIDDEN"})
    kind: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        if not os.path.isfile(self.filepath):
            self.report({"ERROR"}, "File no longer exists - refresh assets")
            return {"CANCELLED"}
        settings = _settings(context.scene)
        place_mode = settings.place_mode if settings else "PLANE"
        if self.kind in ("image", "video") and place_mode == "SAVE_ONLY":
            place_mode = "PLANE"  # explicit click should always do something visible
        try:
            message = _place_received(self.filepath, self.kind, place_mode)
        except Exception as exc:
            self.report({"ERROR"}, f"Place failed: {exc}")
            return {"CANCELLED"}
        _set_status(message)
        return {"FINISHED"}


class GTB_OT_send_asset(bpy.types.Operator):
    bl_idname = "griptape_bridge.send_asset"
    bl_label = "Send to Griptape"
    bl_description = "Send this asset back to Griptape as the next input (e.g. image-to-video, upscale)"

    filepath: StringProperty(options={"HIDDEN"})
    kind: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        if not os.path.isfile(self.filepath):
            self.report({"ERROR"}, "File no longer exists - refresh assets")
            return {"CANCELLED"}
        content_types = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
            ".glb": "model/gltf-binary", ".gltf": "model/gltf+json",
        }
        ext = os.path.splitext(self.filepath)[1].lower()
        try:
            with open(self.filepath, "rb") as f:
                data = f.read()
            result = _post_media(
                data,
                filename=os.path.basename(self.filepath),
                kind=self.kind or "image",
                content_type=content_types.get(ext, "application/octet-stream"),
                meta=_shot_meta(context.scene),
            )
            _set_status(f"Sent '{os.path.basename(self.filepath)}' to GTN (v{result.get('version', '?')}).")
        except Exception as exc:
            self.report({"ERROR"}, f"Send failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GTB_OT_apply_asset_material(bpy.types.Operator):
    bl_idname = "griptape_bridge.apply_asset_material"
    bl_label = "Apply as Material"
    bl_description = "Create a textured material from this image and apply it to the active object"

    filepath: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            self.report({"ERROR"}, "Select a mesh object first")
            return {"CANCELLED"}
        try:
            image = bpy.data.images.load(self.filepath, check_existing=True)
        except Exception as exc:
            self.report({"ERROR"}, f"Cannot load image: {exc}")
            return {"CANCELLED"}

        material = bpy.data.materials.new(name=f"GTN_{os.path.splitext(os.path.basename(self.filepath))[0]}")
        material.use_nodes = True
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        bsdf = nodes.get("Principled BSDF")
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = image
        tex.location = (-320, 260)
        if bsdf is not None:
            links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        # No UVs on the mesh -> fall back to generated coordinates so the
        # texture shows up instead of rendering solid black.
        if not obj.data.uv_layers:
            coords = nodes.new("ShaderNodeTexCoord")
            coords.location = (-540, 260)
            links.new(coords.outputs["Generated"], tex.inputs["Vector"])

        if obj.data.materials:
            obj.data.materials[obj.active_material_index] = material
        else:
            obj.data.materials.append(material)

        # Make the result visible right away.
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                for space in area.spaces:
                    if space.type == "VIEW_3D" and space.shading.type == "SOLID":
                        space.shading.type = "MATERIAL"
        _set_status(f"Material applied to {obj.name}.")
        return {"FINISHED"}


class GTB_OT_apply_asset_hdri(bpy.types.Operator):
    bl_idname = "griptape_bridge.apply_asset_hdri"
    bl_label = "Set as World HDRI"
    bl_description = "Use this image as the world environment texture"

    filepath: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        try:
            image = bpy.data.images.load(self.filepath, check_existing=True)
        except Exception as exc:
            self.report({"ERROR"}, f"Cannot load image: {exc}")
            return {"CANCELLED"}
        world = context.scene.world
        if world is None:
            world = bpy.data.worlds.new("GTN World")
            context.scene.world = world
        world.use_nodes = True
        nodes = world.node_tree.nodes
        links = world.node_tree.links
        nodes.clear()
        env = nodes.new("ShaderNodeTexEnvironment")
        env.image = image
        env.location = (-300, 0)
        background = nodes.new("ShaderNodeBackground")
        output = nodes.new("ShaderNodeOutputWorld")
        output.location = (300, 0)
        links.new(env.outputs["Color"], background.inputs["Color"])
        links.new(background.outputs["Background"], output.inputs["Surface"])
        _set_status(f"World HDRI set: {os.path.basename(self.filepath)}")
        return {"FINISHED"}


class GTB_OT_delete_asset(bpy.types.Operator):
    bl_idname = "griptape_bridge.delete_asset"
    bl_label = "Delete Asset"
    bl_description = "Delete this file from the bridge folder"
    bl_options = {"INTERNAL"}

    filepath: StringProperty(options={"HIDDEN"})

    def invoke(self, context, _event):
        return context.window_manager.invoke_confirm(self, _event)

    def execute(self, _context):
        try:
            os.remove(self.filepath)
        except OSError as exc:
            self.report({"ERROR"}, f"Delete failed: {exc}")
            return {"CANCELLED"}
        for thumb_ext in (".thumb.webp", ".thumb.png", ".thumb.jpg", ".thumb.jpeg"):
            try:
                os.remove(self.filepath + thumb_ext)
            except OSError:
                pass
        _scan_assets()
        _set_status(f"Deleted {os.path.basename(self.filepath)}.")
        return {"FINISHED"}


class GTB_OT_refresh_catalog(bpy.types.Operator):
    bl_idname = "griptape_bridge.refresh_catalog"
    bl_label = "Refresh Node Catalog"
    bl_description = "Fetch the list of node types registered in the GTN engine"

    def execute(self, _context):
        global _catalog_nodes, _auth_checked
        _auth_checked = False  # net loop re-pulls auth + LLM list on next tick
        try:
            payload = _http_json("/proxy/nodes?force=true", timeout=30.0)
            if not payload.get("ok"):
                raise RuntimeError(payload.get("error", "unknown error"))
            nodes = []
            for lib in payload.get("libraries", []):
                for entry in lib.get("nodes", []):
                    full = {**entry, "library": lib.get("library", "")}
                    full["_bucket"] = entry.get("tab") or _bucket_for(full)
                    nodes.append(full)
            # Curated nodes first, in the order they appear in curated_nodes.json.
            nodes.sort(key=lambda n: (n.get("curated_order", 9999), n.get("category", ""), n.get("display_name", "")))
            _catalog_nodes = nodes
            _choice_enum_cache.clear()
            curated_count = sum(1 for n in nodes if n.get("curated"))
            _set_status(f"Catalog loaded: {len(nodes)} node types ({curated_count} curated).")
        except Exception as exc:
            _set_status(f"Catalog refresh failed: {exc}")
            self.report({"ERROR"}, f"Catalog refresh failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GTB_OT_load_params(bpy.types.Operator):
    bl_idname = "griptape_bridge.load_params"
    bl_label = "Load Parameters"
    bl_description = "Fetch the selected node's parameters and build the form"

    def execute(self, context):
        settings = _settings(context.scene)
        selection = settings.proxy_node
        if not selection or selection == "__none__":
            self.report({"ERROR"}, "Pick a node first (refresh the catalog if empty)")
            return {"CANCELLED"}
        library, _, node_type = selection.partition("||")
        try:
            payload = _http_json(
                f"/proxy/schema?library={urllib.parse.quote(library)}&node_type={urllib.parse.quote(node_type)}",
                timeout=30.0,
            )
            if not payload.get("ok"):
                raise RuntimeError(payload.get("error", "unknown error"))

            # Snapshot old values so switching models keeps what still fits
            # (prompt text, seeds, valid enum picks, media sources).
            previous = {}
            for old in settings.proxy_params:
                previous[old.name] = {
                    "ui_type": old.ui_type,
                    "string": old.string_value,
                    "int": old.int_value,
                    "float": old.float_value,
                    "bool": old.bool_value,
                    "enum": old.enum_value,
                    "source": old.attach_source,
                    "file": old.file_path,
                }

            settings.proxy_params.clear()
            for param in payload.get("params", []):
                item = settings.proxy_params.add()
                item.name = param.get("name", "")
                item.label = param.get("label", "") or item.name
                item.tooltip = (param.get("tooltip", "") or "")[:200]
                item.ui_type = param.get("ui_type", "string")
                item.multiline = bool(param.get("multiline", False))
                choices = param.get("choices") or []
                item.choices_json = json.dumps(choices)
                default = param.get("default")
                try:
                    if item.ui_type == "int" and default is not None:
                        item.int_value = int(default)
                    elif item.ui_type == "float" and default is not None:
                        item.float_value = float(default)
                    elif item.ui_type == "bool":
                        item.bool_value = bool(default)
                    elif item.ui_type == "enum" and default is not None:
                        item.enum_value = str(default)
                    elif item.ui_type in ("string", "json") and default is not None:
                        item.string_value = default if isinstance(default, str) else json.dumps(default)
                except Exception:
                    pass

                # Carry compatible previous values over the defaults.
                prev = previous.get(item.name)
                if not prev or prev["ui_type"] != item.ui_type:
                    continue
                try:
                    if item.ui_type in ("string", "json") and prev["string"].strip():
                        item.string_value = prev["string"]
                    elif item.ui_type == "int":
                        item.int_value = prev["int"]
                    elif item.ui_type == "float":
                        item.float_value = prev["float"]
                    elif item.ui_type == "bool":
                        item.bool_value = prev["bool"]
                    elif item.ui_type == "enum" and prev["enum"] in {str(c) for c in choices}:
                        item.enum_value = prev["enum"]
                    elif item.ui_type == "media":
                        item.attach_source = prev["source"]
                        item.file_path = prev["file"]
                except Exception:
                    pass
            settings.proxy_loaded_for = selection
            settings.proxy_job_status = ""
            _set_status(f"Loaded {len(settings.proxy_params)} parameters for '{node_type}'.")
        except Exception as exc:
            _set_status(f"Schema load failed: {exc}")
            self.report({"ERROR"}, f"Schema load failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GTB_OT_run_proxy(bpy.types.Operator):
    bl_idname = "griptape_bridge.run_proxy"
    bl_label = "Generate"
    bl_description = "Run the loaded GTN node with these parameters (result auto-places when it lands)"

    def execute(self, context):
        global _active_job_id
        scene = context.scene
        settings = _settings(scene)
        selection = settings.proxy_loaded_for
        if not selection:
            self.report({"ERROR"}, "Load a node's parameters first")
            return {"CANCELLED"}
        library, _, node_type = selection.partition("||")

        params = {}
        attach_param = ""
        attach_source = "NONE"
        attach_file = ""
        for item in settings.proxy_params:
            if item.ui_type == "media":
                if item.attach_source != "NONE" and not attach_param:
                    attach_param = item.name
                    attach_source = item.attach_source
                    attach_file = item.file_path
                continue
            if item.ui_type == "int":
                params[item.name] = item.int_value
            elif item.ui_type == "float":
                params[item.name] = item.float_value
            elif item.ui_type == "bool":
                params[item.name] = item.bool_value
            elif item.ui_type == "enum":
                if item.enum_value and item.enum_value != "__none__":
                    params[item.name] = item.enum_value
            elif item.ui_type == "json":
                text = item.string_value.strip()
                if text:
                    try:
                        params[item.name] = json.loads(text)
                    except Exception:
                        params[item.name] = text
            else:
                if item.string_value.strip():
                    params[item.name] = item.string_value

        try:
            # A media param with a source pushes fresh media to the bridge
            # right before the run; the engine feeds it into that parameter.
            if attach_param:
                if attach_source == "FILE":
                    path = bpy.path.abspath(attach_file)
                    if not os.path.isfile(path):
                        self.report({"ERROR"}, f"File not found: {attach_file}")
                        return {"CANCELLED"}
                    ext = os.path.splitext(path)[1].lower()
                    content_types = {
                        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
                    }
                    with open(path, "rb") as f:
                        data = f.read()
                    _post_media(
                        data,
                        filename=os.path.basename(path),
                        kind=_asset_kind_for(path) or "image",
                        content_type=content_types.get(ext, "application/octet-stream"),
                        meta=_shot_meta(scene),
                    )
                else:
                    data = _capture_opengl(
                        scene,
                        settings.capture_width,
                        settings.capture_height,
                        view_context=(attach_source == "VIEWPORT"),
                    )
                    _post_media(
                        data,
                        filename=f"blender_capture_{scene.frame_current:04d}.png",
                        kind="image",
                        content_type="image/png",
                        meta=_shot_meta(scene),
                    )

            payload = _http_post_json(
                "/proxy/run",
                {"library": library, "node_type": node_type, "params": params, "attach_param": attach_param},
                timeout=30.0,
            )
            if not payload.get("ok"):
                raise RuntimeError(payload.get("error", "unknown error"))
            _active_job_id = payload.get("id", "")
            settings.proxy_job_status = f"Running '{node_type}'..."
            _set_status(f"Job submitted for '{node_type}'.")
        except Exception as exc:
            settings.proxy_job_status = ""
            _set_status(f"Generate failed: {exc}")
            self.report({"ERROR"}, f"Generate failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GTB_OT_get_latest(bpy.types.Operator):
    bl_idname = "griptape_bridge.get_latest"
    bl_label = "Get Latest"
    bl_description = "Fetch the newest media from GTN and place it using the selected mode"

    def execute(self, _context):
        try:
            _pull_latest(force=True)
        except Exception as exc:
            _set_status(f"Receive failed: {exc}")
            self.report({"ERROR"}, f"Receive failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def _draw_proxy_ui(layout, settings, compact: bool = False) -> None:
    """The 'pick a GTN node, fill its params, Generate' form.

    Shared between the sidebar panel and the floating bar popover.
    """
    tabs = layout.row(align=True)
    tabs.prop(settings, "proxy_tab", expand=True)
    row = layout.row(align=True)
    row.prop(settings, "proxy_filter", text="", icon="VIEWZOOM")
    row.prop(settings, "proxy_curated", text="", icon="SOLO_ON", toggle=True)
    row.operator("griptape_bridge.refresh_catalog", text="", icon="FILE_REFRESH")
    if not _catalog_nodes:
        layout.label(text="Refresh to list GTN nodes", icon="INFO")
        return

    layout.prop(settings, "proxy_node", text="")
    layout.operator("griptape_bridge.load_params", icon="IMPORT")
    if not settings.proxy_loaded_for:
        return

    # Prompt-like fields get top billing: their own box, a tall full-width
    # field, and an expand button that opens a wide dialog for long prompts.
    def _is_prompt(item) -> bool:
        return item.ui_type in ("string", "json") and (item.multiline or "prompt" in item.name.lower())

    for index, item in enumerate(settings.proxy_params):
        if not _is_prompt(item):
            continue
        pbox = layout.box()
        header = pbox.row(align=True)
        header.label(text=item.label, icon="OUTLINER_OB_FONT")
        op = header.operator("griptape_bridge.edit_prompt", text="", icon="FULLSCREEN_ENTER")
        op.param_index = index
        field = pbox.column()
        field.scale_y = 1.4
        field.prop(item, "string_value", text="")
        _draw_prompt_text(pbox, item.string_value)

    col = layout.column(align=True)
    for item in settings.proxy_params:
        if _is_prompt(item):
            continue
        if item.ui_type == "media":
            media_row = col.row(align=True)
            media_row.label(text=item.label, icon="IMAGE_DATA")
            media_row.prop(item, "attach_source", text="")
            if item.attach_source == "FILE":
                col.prop(item, "file_path", text="")
        elif item.ui_type == "enum":
            col.prop(item, "enum_value", text=item.label)
        elif item.ui_type == "int":
            col.prop(item, "int_value", text=item.label)
        elif item.ui_type == "float":
            col.prop(item, "float_value", text=item.label)
        elif item.ui_type == "bool":
            col.prop(item, "bool_value", text=item.label)
        else:
            col.prop(item, "string_value", text=item.label)

    run_row = layout.row()
    run_row.scale_y = 1.6 if compact else 1.4
    run_row.enabled = not bool(_active_job_id)
    run_row.operator("griptape_bridge.run_proxy", icon="PLAY")
    if settings.proxy_job_status:
        layout.label(text=settings.proxy_job_status[:64], icon="TIME" if _active_job_id else "INFO")


_ASSET_KIND_ICONS = {"image": "IMAGE_DATA", "video": "FILE_MOVIE", "model": "FILE_3D"}


class GTB_OT_phonecam_toggle(bpy.types.Operator):
    bl_idname = "griptape_bridge.phonecam_toggle"
    bl_label = "Phone Camera"
    bl_description = "Start/stop the phone virtual camera server (phone drives the active camera)"

    def execute(self, _context):
        try:
            if phonecam.running():
                phonecam.stop()
                _set_status("Phone camera stopped.")
            else:
                cam_url = phonecam.start()
                _set_status(f"Phone camera: open {cam_url} on your phone (same Wi-Fi).")
                self.report({"INFO"}, f"Open {cam_url} on your phone")
        except OSError as exc:
            self.report({"ERROR"}, f"Could not start phone camera: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GTB_OT_phonecam_copy_url(bpy.types.Operator):
    bl_idname = "griptape_bridge.phonecam_copy_url"
    bl_label = "Copy URL"
    bl_description = "Copy the phone camera URL to the clipboard"

    def execute(self, context):
        context.window_manager.clipboard = phonecam.url()
        self.report({"INFO"}, "URL copied")
        return {"FINISHED"}


def _draw_phonecam_ui(layout) -> None:
    box = layout.box()
    box.label(text="Phone Camera", icon="CAMERA_DATA")
    row = box.row()
    row.scale_y = 1.3
    if phonecam.running():
        row.operator("griptape_bridge.phonecam_toggle", text="Stop Phone Camera", icon="PAUSE", depress=True)
        url_row = box.row(align=True)
        url_row.label(text=phonecam.url(), icon="URL")
        url_row.operator("griptape_bridge.phonecam_copy_url", text="", icon="COPYDOWN")
        if phonecam.is_recording():
            box.label(text="RECORDING keyframes...", icon="REC")
        elif phonecam.phone_connected():
            box.label(text="Phone connected - driving the camera", icon="CHECKMARK")
        else:
            box.label(text="Open the URL on your phone (same Wi-Fi),", icon="INFO")
            box.label(text="accept the certificate warning once.")
    else:
        row.operator("griptape_bridge.phonecam_toggle", text="Start Phone Camera", icon="PLAY")


class GTB_OT_refresh_models(bpy.types.Operator):
    bl_idname = "griptape_bridge.refresh_models"
    bl_label = "Refresh LLM List"
    bl_description = "Re-fetch the list of available LLMs from the engine"

    def execute(self, _context):
        try:
            payload = _http_json("/agent/models", timeout=15.0)
            if not payload.get("ok"):
                raise RuntimeError(payload.get("error", "unknown error"))
            _agent_model_items.clear()
            for name in payload.get("models") or []:
                name = str(name)
                _agent_model_items.append((name, name, f"Griptape Cloud model '{name}'"))
            self.report({"INFO"}, f"{len(_agent_model_items)} models available")
        except Exception as exc:
            self.report({"ERROR"}, f"Model refresh failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GTB_OT_run_scene_agent(bpy.types.Operator):
    bl_idname = "griptape_bridge.run_scene_agent"
    bl_label = "Build Scene"
    bl_description = "A Griptape agent assembles the scene using Blender tools (add, move, camera, python)"

    def execute(self, context):
        global _agent_job_id
        settings = _settings(context.scene)
        prompt = settings.agent_prompt.strip()
        if not prompt:
            self.report({"WARNING"}, "Describe the scene first")
            return {"CANCELLED"}
        if _agent_job_id:
            self.report({"WARNING"}, "The scene agent is already working")
            return {"CANCELLED"}
        # The agent's tool calls arrive through the command queue, so make
        # sure this Blender instance is actually listening for them.
        settings.listen_commands = True
        _flags["listen"] = True
        try:
            model = "" if settings.agent_model == "DEFAULT" else settings.agent_model
            payload = _http_post_json("/agent/run", {"prompt": prompt, "model": model})
            if not payload.get("ok"):
                raise RuntimeError(payload.get("error", "unknown error"))
            _agent_job_id = str(payload.get("id", ""))
            _flags["agent_job_id"] = _agent_job_id
            settings.agent_status = "Starting agent..."
            _set_status("Scene agent started.")
        except Exception as exc:
            self.report({"ERROR"}, f"Could not start the scene agent: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GTB_OT_cancel_scene_agent(bpy.types.Operator):
    bl_idname = "griptape_bridge.cancel_scene_agent"
    bl_label = "Cancel Agent"
    bl_description = "Stop the scene agent and drop any commands it still has queued"

    def execute(self, context):
        global _agent_job_id
        job_id = _agent_job_id
        _agent_job_id = ""  # stop polling/showing 'working' immediately
        _flags["agent_job_id"] = ""
        settings = _settings(context.scene)
        settings.agent_status = "Cancelled."
        _set_status("Scene agent cancelled.")
        if job_id:
            try:
                _http_post_json("/agent/cancel", {"id": job_id})
            except Exception:
                pass  # engine unreachable; the job will just finish unobserved
        return {"FINISHED"}


def _draw_agent_ui(layout, settings, compact: bool = False) -> None:
    box = layout.box()
    header = box.row(align=True)
    header.label(text="Describe the scene to build:", icon="OUTLINER_OB_LIGHT")
    header.operator("griptape_bridge.edit_agent_prompt", text="", icon="FULLSCREEN_ENTER")
    col = box.column(align=True)
    col.scale_y = 1.2
    col.prop(settings, "agent_prompt", text="")
    _draw_prompt_text(box, settings.agent_prompt)
    model_row = box.row(align=True)
    model_row.label(text="", icon="EXPERIMENTAL")
    model_row.prop(settings, "agent_model", text="")
    model_row.operator("griptape_bridge.refresh_models", text="", icon="FILE_REFRESH")

    run_row = layout.row(align=True)
    run_row.scale_y = 1.4
    if _agent_job_id:
        working = run_row.row(align=True)
        working.enabled = False
        working.operator("griptape_bridge.run_scene_agent", text="Working...", icon="TIME")
        run_row.operator("griptape_bridge.cancel_scene_agent", text="", icon="X")
    else:
        run_row.operator("griptape_bridge.run_scene_agent", text="Build Scene with Agent", icon="COMMUNITY")
    if settings.agent_status:
        layout.label(text=settings.agent_status[:80], icon="TIME" if _agent_job_id else "INFO")
    if not _prefs().allow_python:
        layout.label(text="Tip: enable 'Allow Python Commands' in prefs for materials/lights", icon="INFO")


def _draw_assets_ui(layout, settings, compact: bool = False) -> None:
    """Thumbnail grid of everything in the bridge folder (Higgsfield-style Assets tab)."""
    row = layout.row(align=True)
    row.prop(settings, "asset_kind", expand=True)
    row.separator()
    row.operator("griptape_bridge.refresh_assets", text="", icon="FILE_REFRESH")
    row.operator("griptape_bridge.open_asset_folder", text="", icon="FILE_FOLDER")

    if not _assets:
        layout.label(text="No assets yet - generate something!", icon="INFO")
        return

    kind_filter = settings.asset_kind
    filtered = [a for a in _assets if kind_filter == "ALL" or a["kind"] == kind_filter]
    if not filtered:
        layout.label(text="No assets of this kind yet.", icon="INFO")
        return

    limit = 24 if compact else 60
    grid = layout.grid_flow(row_major=True, columns=3 if compact else 2, even_columns=True)
    for asset in filtered[:limit]:
        cell = grid.box().column(align=True)
        if asset["icon_id"]:
            cell.template_icon(icon_value=asset["icon_id"], scale=5.0)
        else:
            icon_box = cell.column()
            icon_box.scale_y = 2.5
            icon_box.label(text="", icon=_ASSET_KIND_ICONS.get(asset["kind"], "FILE"))
        name = asset["name"]
        cell.label(text=(name[:22] + "...") if len(name) > 25 else name)
        actions = cell.row(align=True)
        op = actions.operator("griptape_bridge.place_asset", text="Place", icon="IMPORT")
        op.filepath, op.kind = asset["path"], asset["kind"]
        if asset["kind"] == "image":
            op = actions.operator("griptape_bridge.apply_asset_material", text="", icon="MATERIAL")
            op.filepath = asset["path"]
            op = actions.operator("griptape_bridge.apply_asset_hdri", text="", icon="WORLD")
            op.filepath = asset["path"]
        op = actions.operator("griptape_bridge.send_asset", text="", icon="EXPORT")
        op.filepath, op.kind = asset["path"], asset["kind"]
        op = actions.operator("griptape_bridge.delete_asset", text="", icon="TRASH")
        op.filepath = asset["path"]

    if len(filtered) > limit:
        layout.label(text=f"...and {len(filtered) - limit} more (Open Folder)", icon="FILE_FOLDER")


class GTB_PT_floating(bpy.types.Panel):
    """Floating Griptape bar, summoned with Ctrl+Alt+G over the viewport."""

    bl_label = "Griptape"
    bl_idname = "GTB_PT_floating"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_ui_units_x = 22

    def draw(self, context):
        layout = self.layout
        settings = _settings(context.scene)

        header = layout.row(align=True)
        icon = "LINKED" if settings.connected else "UNLINKED"
        header.label(text="Griptape", icon=icon)
        header.prop(settings, "float_tab", expand=True)
        header.operator("griptape_bridge.send_camera_view", text="", icon="OUTLINER_OB_CAMERA")
        header.operator("griptape_bridge.send_viewport", text="", icon="RESTRICT_VIEW_OFF")

        if settings.connected and not settings.signed_in:
            warn = layout.row()
            warn.scale_y = 1.2
            warn.operator("griptape_bridge.sign_in", text="Sign in to run models", icon="KEYINGSET")

        if settings.float_tab == "ASSETS":
            _draw_assets_ui(layout, settings, compact=True)
        elif settings.float_tab == "AGENT":
            _draw_agent_ui(layout, settings, compact=True)
        else:
            layout.prop(settings, "prompt", text="")
            _draw_proxy_ui(layout, settings, compact=True)

        footer = layout.row(align=True)
        footer.prop(settings, "place_mode", text="")
        footer.operator("griptape_bridge.get_latest", text="", icon="SORT_ASC")


class GTB_PT_panel(bpy.types.Panel):
    bl_label = "Griptape Bridge"
    bl_idname = "GTB_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Griptape"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context.scene)
        prefs = _prefs()

        box = layout.box()
        row = box.row(align=True)
        if settings.connected:
            row.label(text=f"Griptape engine online ({prefs.host}:{prefs.port})", icon="LINKED")
        else:
            row.label(text=f"Engine offline ({prefs.host}:{prefs.port})", icon="UNLINKED")
        row.operator("griptape_bridge.test_connection", text="", icon="FILE_REFRESH")
        row.operator("griptape_bridge.open_griptape", text="", icon="URL")
        if not settings.connected:
            box.label(text="Start the GTN engine, or sign in:", icon="INFO")
            box.operator("griptape_bridge.open_griptape", icon="WORLD")
        else:
            auth_row = box.row(align=True)
            if settings.signed_in:
                hint = f" ({settings.key_hint})" if settings.key_hint else ""
                auth_row.label(text=f"Signed in{hint}", icon="CHECKMARK")
                auth_row.operator("griptape_bridge.sign_in", text="", icon="KEYINGSET")
            else:
                auth_row.label(text="Not signed in", icon="ERROR")
                sign_row = box.row()
                sign_row.scale_y = 1.3
                sign_row.operator("griptape_bridge.sign_in", icon="KEYINGSET")

        box = layout.box()
        box.label(text="Send to Griptape", icon="EXPORT")
        box.prop(settings, "prompt", text="")
        row = box.row(align=True)
        row.prop(settings, "capture_width")
        row.prop(settings, "capture_height")
        col = box.column(align=True)
        col.operator("griptape_bridge.send_viewport", icon="RESTRICT_VIEW_OFF")
        col.operator("griptape_bridge.send_camera_view", icon="OUTLINER_OB_CAMERA")
        col.operator("griptape_bridge.send_image_file", icon="IMAGE_DATA")
        col.operator("griptape_bridge.send_video_file", icon="FILE_MOVIE")
        col.operator("griptape_bridge.send_animation", icon="RENDER_ANIMATION")

        box = layout.box()
        box.label(text="Receive from Griptape", icon="IMPORT")
        box.prop(settings, "auto_poll")
        box.prop(settings, "place_mode", text="")
        box.operator("griptape_bridge.get_latest", icon="SORT_ASC")
        if settings.last_received:
            box.label(text=settings.last_received, icon="CHECKMARK")

        box = layout.box()
        row = box.row(align=True)
        row.label(text="Run Griptape Node", icon="NODETREE")
        row.label(text="(header button floats this)")
        _draw_proxy_ui(box, settings)

        box = layout.box()
        box.label(text="Scene Agent", icon="COMMUNITY")
        _draw_agent_ui(box, settings)
        box.prop(settings, "listen_commands")
        if settings.listen_commands and not prefs.allow_python:
            box.label(text="run_python disabled (see prefs)", icon="INFO")

        _draw_phonecam_ui(layout)

        layout.label(text=settings.status or "")


class GTB_PT_assets(bpy.types.Panel):
    bl_label = "Assets"
    bl_idname = "GTB_PT_assets"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Griptape"

    def draw(self, context):
        _draw_assets_ui(self.layout, _settings(context.scene))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
_CLASSES = (
    GriptapeBridgePreferences,
    GriptapeProxyParam,
    GriptapeBridgeSettings,
    GTB_OT_test_connection,
    GTB_OT_open_griptape,
    GTB_OT_sign_in,
    GTB_OT_send_viewport,
    GTB_OT_send_camera_view,
    GTB_OT_send_image_file,
    GTB_OT_send_video_file,
    GTB_OT_send_animation,
    GTB_OT_edit_prompt,
    GTB_OT_edit_agent_prompt,
    GTB_OT_refresh_assets,
    GTB_OT_open_asset_folder,
    GTB_OT_place_asset,
    GTB_OT_send_asset,
    GTB_OT_apply_asset_material,
    GTB_OT_apply_asset_hdri,
    GTB_OT_delete_asset,
    GTB_OT_refresh_catalog,
    GTB_OT_load_params,
    GTB_OT_run_proxy,
    GTB_OT_phonecam_toggle,
    GTB_OT_phonecam_copy_url,
    GTB_OT_refresh_models,
    GTB_OT_run_scene_agent,
    GTB_OT_cancel_scene_agent,
    GTB_OT_get_latest,
    GTB_PT_floating,
    GTB_PT_panel,
    GTB_PT_assets,
)

def _header_button(self, context):
    """Tiny Griptape button in the 3D viewport header (like the Higgsfield icon).

    Doubles as a status capsule: while a job runs it shows 'Generating...'
    so you see progress even with the popover closed.
    """
    if context.area.type != "VIEW_3D":
        return
    settings = _settings(context.scene)
    if _active_job_id:
        self.layout.popover(panel="GTB_PT_floating", text="Generating...", icon="SORTTIME")
    elif settings and settings.connected:
        self.layout.popover(panel="GTB_PT_floating", text="Griptape", icon="LINKED")
    else:
        self.layout.popover(panel="GTB_PT_floating", text="Griptape", icon="NODETREE")


_keymaps: list = []


def _register_keymap() -> None:
    try:
        kc = bpy.context.window_manager.keyconfigs.addon
        if kc is None:
            return
        km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
        # Ctrl+Alt+G is Blender's 'Remove from Collection'; use Shift too.
        kmi = km.keymap_items.new("wm.call_panel", type="G", value="PRESS", ctrl=True, alt=True, shift=True)
        kmi.properties.name = "GTB_PT_floating"
        kmi.properties.keep_open = True
        _keymaps.append((km, kmi))
    except Exception:
        pass


def _unregister_keymap() -> None:
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _keymaps.clear()


def _initial_asset_scan():
    try:
        _scan_assets()
    except Exception:
        pass
    return None  # one-shot timer


def register():
    global _net_thread
    phonecam._capture = _capture_opengl
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.griptape_bridge = PointerProperty(type=GriptapeBridgeSettings)
    if not bpy.app.timers.is_registered(_main_pump):
        bpy.app.timers.register(_main_pump, first_interval=MAIN_PUMP_INTERVAL, persistent=True)
    bpy.app.timers.register(_initial_asset_scan, first_interval=1.0)
    _net_stop.clear()
    if _net_thread is None or not _net_thread.is_alive():
        _net_thread = threading.Thread(target=_net_loop, name="griptape-bridge-net", daemon=True)
        _net_thread.start()
    _register_keymap()
    bpy.types.VIEW3D_HT_header.append(_header_button)


def unregister():
    global _net_thread, _asset_previews
    try:
        phonecam.stop()
    except Exception:
        pass
    try:
        bpy.types.VIEW3D_HT_header.remove(_header_button)
    except Exception:
        pass
    if _asset_previews is not None:
        try:
            bpy.utils.previews.remove(_asset_previews)
        except Exception:
            pass
        _asset_previews = None
        _assets.clear()
    _unregister_keymap()
    _net_stop.set()
    _net_thread = None
    if bpy.app.timers.is_registered(_main_pump):
        bpy.app.timers.unregister(_main_pump)
    del bpy.types.Scene.griptape_bridge
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
