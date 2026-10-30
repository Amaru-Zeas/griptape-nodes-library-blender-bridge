"""Phone-as-virtual-camera for the Griptape Bridge add-on.

Runs a small HTTPS server on the LAN (self-signed cert -- phones only expose
gyro/motion APIs to secure pages). The phone opens a web page, streams gyro +
touch poses via POST ~20x/sec, and Blender applies them to the active camera,
anchored to wherever the camera was when the phone connected. A Record button
bakes real-time keyframes; the page shows a low-fps render preview through the
camera so the phone doubles as a virtual monitor.

Everything here is Blender-side; the Griptape engine is not involved.
"""

import base64
import json
import math
import os
import socket
import ssl
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bpy
from mathutils import Quaternion, Vector

PORT = 7899
APPLY_INTERVAL = 1.0 / 30.0
PREVIEW_INTERVAL = 0.7
PREVIEW_WIDTH = 480
PHONE_ACTIVE_WINDOW = 5.0  # seconds since last pose to consider a phone connected

# Self-signed cert (CN=griptape-phonecam, valid 10 years). Only used to give the
# LAN page a secure context so mobile browsers allow the motion sensors; the
# phone shows a one-time certificate warning that the user accepts.
_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIC3jCCAcagAwIBAgIUWAS9c8ffUNPX0FkCgMDDiWu/OwQwDQYJKoZIhvcNAQEL
BQAwHDEaMBgGA1UEAwwRZ3JpcHRhcGUtcGhvbmVjYW0wHhcNMjYwOTAyMDI0ODIx
WhcNMzYwODMxMDI0ODIxWjAcMRowGAYDVQQDDBFncmlwdGFwZS1waG9uZWNhbTCC
ASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAM0PWUpC7GnUHByzP97SwcZS
p6mMwR04pHzIcKBWFU3YrWwYaPo3ljk6bZxzYqT+uoO6I0Xh3kdU8f1SgiRjhkod
IPZE1X9YTS+yn//r767TGcBq/koXxUg3cgL7li2zrix5pD2hahE60PLvouXR+fXV
eqrTbpG9HXwm112fSslYhW8VbA1TSRcl8owA8zQvddyrEzgsn9eEelhCkqyfupUY
eZeQrl2fzhTbVqr0O5u7APx7ZhtoIcgSkYVzawQn6JqtBSMDqAZFDjMI/AMUK17U
0DG4lhNUCuu9NQ2hdmtFtIRsXNsa0T0NAn6JQ96cjnS/W8fqVUc4yK4h8UZHXi0C
AwEAAaMYMBYwFAYDVR0RBA0wC4IJbG9jYWxob3N0MA0GCSqGSIb3DQEBCwUAA4IB
AQAyoGWpb8Ypt5sDBJgzCETSfGHOLXJMz/TdblLC11yKwNqifS5vJhDcFZlG/3fO
DXUv4YiCHP+YMdYNyCqEiu8aipOOYqc7yCG69GxlMhFrL6RiNaiw8zFTfh04gmaZ
Tmd6gtwg/SuKrbGB/bW+I/5jfCxhuheM48PfmAXJnwhRjcVc72bsc+a67Ae2YQF4
HufeCMlUdD859SNDyuTEHqz02G8rbnyssaUpFkWaPnrALX0Rc6i+ka33UMYCNM/w
QoQ1/aEavlRoARoDQeHkWtMMEWh5GCk+2oq5fypWkNvvDEN12i1MwaIPQRMj9S0z
4SB5TgE+p9m8boKJ1fX5sF8i
-----END CERTIFICATE-----
"""

_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDND1lKQuxp1Bwc
sz/e0sHGUqepjMEdOKR8yHCgVhVN2K1sGGj6N5Y5Om2cc2Kk/rqDuiNF4d5HVPH9
UoIkY4ZKHSD2RNV/WE0vsp//6++u0xnAav5KF8VIN3IC+5Yts64seaQ9oWoROtDy
76Ll0fn11Xqq026RvR18Jtddn0rJWIVvFWwNU0kXJfKMAPM0L3XcqxM4LJ/XhHpY
QpKsn7qVGHmXkK5dn84U21aq9DubuwD8e2YbaCHIEpGFc2sEJ+iarQUjA6gGRQ4z
CPwDFCte1NAxuJYTVArrvTUNoXZrRbSEbFzbGtE9DQJ+iUPenI50v1vH6lVHOMiu
IfFGR14tAgMBAAECggEAH7HDIczFQLg1niQAd77Ij5A/9N9aC9uw5M6QRd/4S/91
EpOVSjNkRzUmMvvMn+b1Sh3vjMIT+p+BBaAGmHw/id4Ftbq+6v9pcrlfrI05jyJ2
xPxkkGwrJHXZa+eZ9zdh8OLeRV5ZCA+4swvh+DajtWEJJDUozmLWF1XjjMdtpj/+
7kCMcHXojVzubQf3wunWWFD86rP7OZyaQVljOP90FQOy7FEeh57CfSg1Q2KgD7EI
xiD8CPsDVl6Zeye+NBWyWtip3ufdbIu1G7g8tQWL4QDf82UZTkV2XYFD1ID7qtRn
ZerlZHa/DwW2DjQWElY4NCY3vVJO9enMimGqUMhWgQKBgQDzi7dBoAZZF/tiCXak
wji/4eoLwhEQ+RzCsiVBLmEvt/vAplB3x7IkMxTwBc9IUjCJFVnMUiKed5vR9WIX
/D0Q3k7gWqdTNGKtrn9D70NjynlH3Z3PHNPznlgvSvK4S2MFwHy0cP8B3JIvPlFA
WGoO+C8Ws+M7jH9aZ0GVHwuH7QKBgQDXi8+fbUD7sO9bGNPBEpT8w7bZ2hbPAjIX
j2DLb9jXzqyjtoDaIcr8lsku1y/oTFs8ClElrynhv7DQ7mrurWSbswqpk49cetEC
wf4TlVguQuWso3wl3X+1XxHQ9X+EKpL0Qdmcurmf/xJotAcHPcIfMC3CdIfINOYT
gV1qwx/nQQKBgGme4Bzfz6D325eKzx9o87+TtDLhORGJV8XACJQaDx/oxI+qER/B
RSqZ1Ky7eEe/DTmGPulzfXwIF1WY+6+3PqyAHOkDp6lYAKueD6dgrnMqripSnEvk
Dq+KrmfkMxtl5k3qSqrejlk6VVOTzR0Auw4+5/0KirOanOiR+0k2q4OJAoGAHzmb
zEaxgUSyiAniszeT6cIYfRrA7yAsTYlxxkbfrPO9wsGvjOoUU98sKeL2h7p4BQyh
kMn9UTzJypqfVxA7YOrhAovj2svX94RO4FtkON9sEkRkub+KvqWVGSZXYLgiLcxU
UAYk2e7DpfNeiUgDFS2Pk3n6KaCwK+czt0GpH4ECgYEAoQK7XBXiwvdVXz+USCz5
G9S0qBdAypCu0hlwb+s4ESrfBJjHI8tVbzMQGMY+pHL5esovypI2ge+m8FKh3JKm
N6/uV47onohuZXpPI3JmqniAum6rdoRQgdM7oo5N5J56qd69S4Qo0RGszL+QgI6a
IpfNiNJ8UJ/i8UpMWkMCcNk=
-----END PRIVATE KEY-----
"""

# ---------------------------------------------------------------------------
# Shared state (server threads write, Blender timer reads).
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_server: ThreadingHTTPServer | None = None
_server_thread: threading.Thread | None = None
_latest: dict = {}  # newest pose payload from the phone
_latest_seq = 0
_last_pose_at = 0.0
_status = ""

# Preview: the Blender timer renders into this slot; server threads serve it.
_preview_png: bytes = b""
_preview_at = 0.0

# Injected by __init__ to avoid a circular import.
_capture = None  # _capture_opengl(scene, width, height, view_context) -> png bytes

# Anchor/apply state (main thread only).
_anchor: dict = {}
_recording = False
_rec_t0 = 0.0
_rec_frame0 = 0
_rec_last_frame = -1
_rec_frames = 0


def running() -> bool:
    return _server is not None


def status() -> str:
    return _status


def phone_connected() -> bool:
    return running() and (time.monotonic() - _last_pose_at) < PHONE_ACTIVE_WINDOW


def is_recording() -> bool:
    return _recording


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the outbound interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def url() -> str:
    return f"https://{lan_ip()}:{PORT}"


# ---------------------------------------------------------------------------
# Pose math: W3C device orientation -> Blender camera quaternion.
# Uses the three.js DeviceOrientationControls recipe, then converts the Y-up
# result to Blender's Z-up world. Constant frame offsets cancel out through
# the anchor (we apply deltas relative to the orientation at Recenter).
# ---------------------------------------------------------------------------
_SQ2 = math.sqrt(0.5)
_Q_SCREEN_TO_BACK = Quaternion((_SQ2, -_SQ2, 0.0, 0.0))  # -90 deg about X
_Q_YUP_TO_ZUP = Quaternion((_SQ2, _SQ2, 0.0, 0.0))  # +90 deg about X


def _device_quat(alpha: float, beta: float, gamma: float, screen_deg: float) -> Quaternion:
    x = math.radians(beta or 0.0)
    y = math.radians(alpha or 0.0)
    z = -math.radians(gamma or 0.0)
    c1, c2, c3 = math.cos(x / 2), math.cos(y / 2), math.cos(z / 2)
    s1, s2, s3 = math.sin(x / 2), math.sin(y / 2), math.sin(z / 2)
    # three.js Euler order 'YXZ'
    q = Quaternion(
        (
            c1 * c2 * c3 + s1 * s2 * s3,
            s1 * c2 * c3 + c1 * s2 * s3,
            c1 * s2 * c3 - s1 * c2 * s3,
            c1 * c2 * s3 - s1 * s2 * c3,
        )
    )
    q_screen = Quaternion((0.0, 0.0, 1.0), -math.radians(screen_deg or 0.0))
    return _Q_YUP_TO_ZUP @ (q @ _Q_SCREEN_TO_BACK @ q_screen)


# ---------------------------------------------------------------------------
# Applying poses to the camera (Blender timer, main thread).
# ---------------------------------------------------------------------------
def _active_camera():
    scene = bpy.context.scene
    if scene.camera is None:
        cam_data = bpy.data.cameras.new("PhoneCam")
        cam_obj = bpy.data.objects.new("PhoneCam", cam_data)
        scene.collection.objects.link(cam_obj)
        cam_obj.location = (0.0, -6.0, 1.6)
        cam_obj.rotation_euler = (math.radians(90), 0.0, 0.0)
        scene.camera = cam_obj
    return scene.camera


def _set_anchor(cam, phone_q: Quaternion | None) -> None:
    if cam.rotation_mode != "QUATERNION":
        _anchor["prev_mode"] = cam.rotation_mode
        cam.rotation_mode = "QUATERNION"
    _anchor["base_q"] = cam.rotation_quaternion.copy()
    _anchor["base_loc"] = cam.location.copy()
    _anchor["ref_q"] = phone_q
    # First anchor after Start = "home": the Reset Cam button flies back here.
    if "home_q" not in _anchor:
        _anchor["home_q"] = _anchor["base_q"].copy()
        _anchor["home_loc"] = _anchor["base_loc"].copy()


def _yaw_pitch_of(q: Quaternion, fallback_yaw: float = 0.0) -> tuple:
    """Yaw/pitch of a rotation's view direction (camera looks along local -Z)."""
    f = q @ Vector((0.0, 0.0, -1.0))
    if math.hypot(f.x, f.y) < 1e-4:  # looking straight up/down: yaw undefined
        yaw = fallback_yaw
    else:
        yaw = math.atan2(-f.x, f.y)
    pitch = math.asin(max(-1.0, min(1.0, f.z)))
    return yaw, pitch


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _clear_motion_refs() -> None:
    for key in ("ref_q", "gyro_ref", "lvl_yaw", "lvl_pitch"):
        _anchor.pop(key, None)


def _apply_pose(payload: dict) -> None:
    global _recording, _rec_t0, _rec_frame0, _rec_last_frame, _rec_frames

    cam = _active_camera()
    if "base_q" not in _anchor:
        _set_anchor(cam, None)

    if payload.get("recenter"):
        _set_anchor(cam, None)
        _clear_motion_refs()

    if payload.get("reset") and "home_q" in _anchor:
        _anchor["base_q"] = _anchor["home_q"].copy()
        _anchor["base_loc"] = _anchor["home_loc"].copy()
        _clear_motion_refs()

    gyro = payload.get("gyro")
    touch = payload.get("touch") or {}
    t_yaw = float(touch.get("yaw") or 0.0)
    t_pitch = float(touch.get("pitch") or 0.0)

    if payload.get("level", True):
        # Pan/tilt rig model: drive yaw/pitch from the phone's *view direction*
        # only. Roll never moves the view axis, so bad screen-orientation
        # reporting (landscape iPhones/iPads) cannot bleed tilt into pan, and
        # the horizon stays level by construction.
        if "lvl_yaw" not in _anchor:
            _anchor["lvl_yaw"], _anchor["lvl_pitch"] = _yaw_pitch_of(_anchor["base_q"])
        _anchor["lvl_yaw"] += t_yaw
        _anchor["lvl_pitch"] += t_pitch

        gyro_dyaw = gyro_dpitch = 0.0
        if isinstance(gyro, dict):
            q = _device_quat(
                float(gyro.get("alpha") or 0.0),
                float(gyro.get("beta") or 0.0),
                float(gyro.get("gamma") or 0.0),
                float(gyro.get("screen") or 0.0),
            )
            ref = _anchor.get("gyro_ref")
            phone_yaw, phone_pitch = _yaw_pitch_of(q, fallback_yaw=(ref[0] if ref else 0.0))
            if ref is None:
                ref = (phone_yaw, phone_pitch)
                _anchor["gyro_ref"] = ref
            gyro_dyaw = _wrap_pi(phone_yaw - ref[0])
            gyro_dpitch = phone_pitch - ref[1]

        pitch = max(-1.55, min(1.55, _anchor["lvl_pitch"] + gyro_dpitch))
        rotation = Quaternion((0.0, 0.0, 1.0), _anchor["lvl_yaw"] + gyro_dyaw) @ Quaternion(
            (1.0, 0.0, 0.0), math.pi / 2 + pitch
        )
    else:
        # Full 3DoF handheld mode: layer the phone's rotation delta (roll
        # included) onto the anchored camera orientation.
        if isinstance(gyro, dict):
            q = _device_quat(
                float(gyro.get("alpha") or 0.0),
                float(gyro.get("beta") or 0.0),
                float(gyro.get("gamma") or 0.0),
                float(gyro.get("screen") or 0.0),
            )
            if _anchor.get("ref_q") is None:
                _anchor["ref_q"] = q.copy()
            delta = _anchor["ref_q"].conjugated() @ q
        else:
            delta = Quaternion()
        if t_yaw:
            _anchor["base_q"] = Quaternion((0.0, 0.0, 1.0), t_yaw) @ _anchor["base_q"]
        if t_pitch:
            _anchor["base_q"] = _anchor["base_q"] @ Quaternion((1.0, 0.0, 0.0), t_pitch)
        rotation = _anchor["base_q"] @ delta

    # Movement in current camera axes: dolly along view, truck/pedestal sideways/up.
    move = Vector(
        (
            float(touch.get("truck") or 0.0),
            float(touch.get("pedestal") or 0.0),
            -float(touch.get("dolly") or 0.0),
        )
    )
    if move.length_squared:
        _anchor["base_loc"] = _anchor["base_loc"] + rotation @ move

    cam.rotation_quaternion = rotation
    cam.location = _anchor["base_loc"]

    # Real-time recording: map elapsed wall time onto the timeline.
    want_record = bool(payload.get("record"))
    scene = bpy.context.scene
    if want_record and not _recording:
        _recording = True
        _rec_t0 = time.monotonic()
        _rec_frame0 = scene.frame_current
        _rec_last_frame = -1
        _rec_frames = 0
    elif not want_record and _recording:
        _recording = False

    if _recording:
        fps = scene.render.fps / scene.render.fps_base
        frame = _rec_frame0 + int((time.monotonic() - _rec_t0) * fps)
        if frame != _rec_last_frame:
            scene.frame_set(frame)
            cam.keyframe_insert(data_path="location", frame=frame)
            cam.keyframe_insert(data_path="rotation_quaternion", frame=frame)
            _rec_last_frame = frame
            _rec_frames += 1


def _apply_timer():
    """30 Hz while the server runs: apply the newest phone pose, refresh preview."""
    global _preview_png, _preview_at, _status

    if not running():
        return None  # unregister

    seq_seen = _anchor.get("seq_seen", -1)
    with _lock:
        payload = dict(_latest) if _latest else None
        seq = _latest_seq
    if payload is not None and seq != seq_seen:
        _anchor["seq_seen"] = seq
        try:
            _apply_pose(payload)
        except Exception as exc:
            _status = f"Pose apply failed: {exc}"

    # Camera-view preview for the phone, only while a phone is active.
    if _capture is not None and phone_connected() and (time.monotonic() - _preview_at) > PREVIEW_INTERVAL:
        try:
            scene = bpy.context.scene
            aspect = scene.render.resolution_y / max(1, scene.render.resolution_x)
            png = _capture(scene, PREVIEW_WIDTH, max(2, int(PREVIEW_WIDTH * aspect)), False)
            with _lock:
                _preview_png = png
        except Exception:
            pass
        _preview_at = time.monotonic()

    return APPLY_INTERVAL


# ---------------------------------------------------------------------------
# HTTP layer.
# ---------------------------------------------------------------------------
class _PhoneCamHandler(BaseHTTPRequestHandler):
    server_version = "GTNPhoneCam/0.1"

    def log_message(self, *_args) -> None:  # silence per-request console spam
        pass

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
        elif path == "/preview.png":
            with _lock:
                png = _preview_png
            if png:
                self._send(200, "image/png", png)
            else:
                self._send(404, "text/plain", b"no preview yet")
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):  # noqa: N802
        global _latest_seq, _last_pose_at
        if self.path.split("?", 1)[0] != "/pose":
            self._send(404, "application/json", b'{"ok": false}')
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send(400, "application/json", b'{"ok": false}')
            return
        with _lock:
            _latest.clear()
            _latest.update(payload if isinstance(payload, dict) else {})
            _latest_seq += 1
        _last_pose_at = time.monotonic()
        body = json.dumps({"ok": True, "recording": _recording, "frames": _rec_frames}).encode("utf-8")
        self._send(200, "application/json", body)


def start() -> str:
    """Start the phone camera server. Main thread only. Returns the URL."""
    global _server, _server_thread, _status
    if _server is not None:
        return url()

    # ssl needs cert/key as files; stage the embedded PEMs in the temp folder.
    cert_dir = os.path.join(tempfile.gettempdir(), "gtn_phonecam")
    os.makedirs(cert_dir, exist_ok=True)
    cert_path = os.path.join(cert_dir, "cert.pem")
    key_path = os.path.join(cert_dir, "key.pem")
    with open(cert_path, "w") as f:
        f.write(_CERT_PEM)
    with open(key_path, "w") as f:
        f.write(_KEY_PEM)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), _PhoneCamHandler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.daemon_threads = True

    thread = threading.Thread(target=server.serve_forever, name="gtn-phonecam", daemon=True)
    thread.start()
    _server = server
    _server_thread = thread
    _status = "Phone camera running - open the URL on your phone."

    _anchor.clear()
    if not bpy.app.timers.is_registered(_apply_timer):
        bpy.app.timers.register(_apply_timer, first_interval=APPLY_INTERVAL, persistent=True)
    return url()


def stop() -> None:
    global _server, _server_thread, _status, _recording
    server = _server
    _server = None  # timer sees this and unregisters itself
    _server_thread = None
    _recording = False
    _status = ""
    if server is not None:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
    # Restore the camera's original rotation mode if we changed it.
    prev_mode = _anchor.pop("prev_mode", None)
    if prev_mode:
        try:
            cam = bpy.context.scene.camera
            if cam is not None:
                cam.rotation_mode = prev_mode
        except Exception:
            pass
    _anchor.clear()


# ---------------------------------------------------------------------------
# The phone page. Single file, no dependencies.
# ---------------------------------------------------------------------------
_PAGE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Griptape Phone Cam</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; -webkit-user-select:none; user-select:none; }
  html,body { height:100%; background:#0e0e12; color:#eee; font-family:-apple-system,system-ui,sans-serif;
              overflow:hidden; touch-action:none; }
  #stage { position:relative; width:100%; height:100%; display:flex; flex-direction:column; }
  #view { flex:1; position:relative; background:#17171d; overflow:hidden; }
  #preview { position:absolute; inset:0; width:100%; height:100%; object-fit:contain; opacity:.92;
             pointer-events:none; }
  #hint { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
          text-align:center; color:#777; font-size:14px; padding:24px; pointer-events:none; }
  #bar { display:flex; flex-wrap:wrap; gap:8px; padding:10px; background:#111116; }
  button { flex:1 1 28%; padding:14px 6px; font-size:15px; font-weight:600; border:0; border-radius:12px;
           background:#26262e; color:#eee; white-space:nowrap; }
  button.on { background:#2f6fed; }
  #rec { flex:1 1 100%; }
  #rec.on { background:#d3382c; }
  #status { position:absolute; top:8px; left:0; right:0; text-align:center; font-size:12px;
            color:#9a9aa4; pointer-events:none; text-shadow:0 1px 2px #000; }
</style></head>
<body>
<div id="stage">
  <div id="view">
    <img id="preview" alt="">
    <div id="hint">drag = look &nbsp;&bull;&nbsp; two fingers = move &nbsp;&bull;&nbsp; pinch = dolly</div>
    <div id="status">connecting...</div>
  </div>
  <div id="bar">
    <button id="motion">Enable Motion</button>
    <button id="level" class="on">Level</button>
    <button id="recenter">Recenter</button>
    <button id="reset">Reset Cam</button>
    <button id="rec">&#9210; Record</button>
  </div>
</div>
<script>
var gyro = null, gyroOn = false, record = false, recenter = false, reset = false, levelOn = true;
var touch = {yaw:0, pitch:0, dolly:0, truck:0, pedestal:0};
var lastPointers = {}, pinchDist = 0, sendFails = 0;

function screenAngle() {
  if (screen.orientation && typeof screen.orientation.angle === 'number') return screen.orientation.angle;
  return window.orientation || 0;
}

window.addEventListener('deviceorientation', function (e) {
  if (!gyroOn || e.alpha === null) return;
  gyro = {alpha:e.alpha, beta:e.beta, gamma:e.gamma, screen:screenAngle()};
});

document.getElementById('motion').addEventListener('click', function () {
  var btn = this;
  function enable() { gyroOn = true; btn.classList.add('on'); btn.textContent = 'Motion On'; }
  if (typeof DeviceOrientationEvent !== 'undefined' && DeviceOrientationEvent.requestPermission) {
    DeviceOrientationEvent.requestPermission().then(function (r) { if (r === 'granted') enable(); });
  } else { enable(); }
});

document.getElementById('recenter').addEventListener('click', function () { recenter = true; });
document.getElementById('reset').addEventListener('click', function () { reset = true; });
document.getElementById('level').addEventListener('click', function () {
  levelOn = !levelOn;
  this.classList.toggle('on', levelOn);
});
document.getElementById('rec').addEventListener('click', function () {
  record = !record;
  this.classList.toggle('on', record);
  this.innerHTML = record ? '&#9209; Stop' : '&#9210; Record';
});

var view = document.getElementById('view');
view.addEventListener('pointerdown', function (e) {
  lastPointers[e.pointerId] = {x:e.clientX, y:e.clientY};
  view.setPointerCapture(e.pointerId);
});
view.addEventListener('pointerup', function (e) { delete lastPointers[e.pointerId]; pinchDist = 0; });
view.addEventListener('pointercancel', function (e) { delete lastPointers[e.pointerId]; pinchDist = 0; });
view.addEventListener('pointermove', function (e) {
  var prev = lastPointers[e.pointerId];
  if (!prev) return;
  var dx = e.clientX - prev.x, dy = e.clientY - prev.y;
  lastPointers[e.pointerId] = {x:e.clientX, y:e.clientY};
  var ids = Object.keys(lastPointers);
  if (ids.length === 1) {
    touch.yaw   -= dx * 0.004;   // drag right = look right
    touch.pitch -= dy * 0.004;   // drag up = look up
  } else if (ids.length === 2) {
    var a = lastPointers[ids[0]], b = lastPointers[ids[1]];
    var dist = Math.hypot(a.x - b.x, a.y - b.y);
    if (pinchDist) touch.dolly += (dist - pinchDist) * 0.01;  // pinch out = forward
    pinchDist = dist;
    touch.truck    += dx * 0.005;
    touch.pedestal -= dy * 0.005;
  }
});

var statusEl = document.getElementById('status');
setInterval(function () {
  var t = touch; touch = {yaw:0, pitch:0, dolly:0, truck:0, pedestal:0};
  var body = {gyro: gyroOn ? gyro : null, touch: t, record: record, recenter: recenter,
              reset: reset, level: levelOn};
  recenter = false; reset = false;
  fetch('/pose', {method:'POST', body: JSON.stringify(body), keepalive: true})
    .then(function (r) { return r.json(); })
    .then(function (j) {
      sendFails = 0;
      statusEl.textContent = j.recording ? ('REC ' + j.frames + ' frames') : 'connected';
      statusEl.style.color = j.recording ? '#ff6b5e' : '#9a9aa4';
    })
    .catch(function () { if (++sendFails > 3) statusEl.textContent = 'disconnected'; });
}, 66);

var img = document.getElementById('preview');
var hint = document.getElementById('hint');
setInterval(function () {
  var probe = new Image();
  probe.onload = function () { img.src = probe.src; hint.style.display = 'none'; };
  probe.src = '/preview.png?t=' + Date.now();
}, 800);
</script>
</body></html>
"""
