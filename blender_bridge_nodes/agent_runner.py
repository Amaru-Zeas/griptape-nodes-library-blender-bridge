"""Scene-assembly agent: a Griptape Agent whose tools drive the Blender scene.

The agent runs inside the GTN engine (same process as the bridge). Each tool
call enqueues a command on the bridge's command queue; the Blender add-on
(with 'Listen for Commands' enabled) executes it on the main thread and posts
the result back, which the tool returns to the agent. This gives a full
observe-act loop: the agent can inspect the scene, add/move objects, aim the
camera, and verify its work -- no external MCP client required.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from typing import Any, Optional

COMMAND_TIMEOUT_SECONDS = 90.0
MAX_JOBS_KEPT = 16

_jobs: dict[str, dict] = {}
_lock = threading.Lock()

_SYSTEM_INSTRUCTIONS = """You are a 3D scene assembly artist operating a live Blender scene through tools. You are judged on visual quality, not just correctness -- bare gray primitives are a failure.

Coordinate basics:
- Blender uses meters and a Z-up axis; rotations are Euler radians [x, y, z].
- Ground objects at z >= 0 unless asked otherwise; avoid overlaps and floating objects.
- Give objects descriptive names (Pine_1, Campfire_Rock_3...).

Quality rules (follow all of them):
1. Start with scene_info to see what exists.
2. Never represent a real-world thing with a single bare primitive when a composite reads better. A tree is a cylinder trunk plus 2-3 stacked, slightly offset cones; a tent is a stretched cube plus a different-colored floor flap; rocks are ico-style spheres with non-uniform scale. Vary scale and rotation of repeated objects by 10-30% so nothing looks cloned.
3. Always create materials via run_python: a Principled BSDF per material with a sensible base color, roughness, and (for fire/embers/lamps) emission. Set a world background color that matches the mood. Untextured gray output is unacceptable when run_python works.
4. Light deliberately: a key light (sun or area), a fill, and accent lights with color (warm orange for fire, cool blue for dusk sky). Set light energy and color explicitly.
5. After the main build, aim the camera at the composition, then call view_scene to look at your work. Fix the two or three worst problems it reveals (intersections, dead framing, flat lighting), then view_scene once more if you changed a lot.
6. At the end, use run_python to switch 3D viewport shading so the user sees the result:
   for w in bpy.context.window_manager.windows:
       for a in w.screen.areas:
           if a.type == 'VIEW_3D':
               for s in a.spaces:
                   if s.type == 'VIEW_3D':
                       s.shading.type = 'MATERIAL'
7. If run_python returns an error saying Python commands are disabled, continue with geometry only and tell the user in your summary to enable 'Allow Python Commands' in the add-on preferences for materials and lighting.
8. Finish with one short paragraph: what you built, the mood, and anything the user should tweak.

User request:
"""

_VIEW_FALLBACK_MODEL = "gemini-3.5-flash"

_CRITIQUE_PROMPT = """You are an art director reviewing a Blender viewport render of a work-in-progress 3D scene.
The scene was built toward this goal:
{goal}

Critique the image in under 150 words: composition/framing, lighting, materials/colors, object placement (intersections, floating objects, cloned-looking repeats), and overall mood. List the 3 most impactful concrete fixes first."""


def _bridge():
    """Lazy import; the engine may load this library as top-level modules."""
    try:
        from . import bridge_server
    except ImportError:  # pragma: no cover
        import bridge_server  # type: ignore[no-redef]
    return bridge_server


def _check_cancelled(job: dict) -> None:
    """End the agent quickly once the user cancels.

    Raised as SystemExit on purpose: griptape's tool loop catches Exception and
    would feed the error back to the LLM (which then keeps going); SystemExit
    escapes that and terminates the worker thread. The job status is finalized
    before raising.
    """
    if job.get("cancel"):
        job["status"] = "cancelled"
        raise SystemExit("scene agent cancelled")


def _run_blender_command(command: str, params: dict, job: dict) -> dict:
    """Enqueue a command, wait for Blender to execute it, return the result."""
    _check_cancelled(job)
    bridge_server = _bridge()

    entry = bridge_server.enqueue_command(command, params)
    command_id = entry["id"]
    job["actions"].append({"command": command, "params": params})

    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        _check_cancelled(job)
        current = bridge_server.get_command(command_id)
        if current is not None and current["status"] in ("done", "error"):
            if current["status"] == "error":
                error = (current.get("result") or {}).get("error", "unknown error")
                return {"error": str(error)}
            return current.get("result") or {}
        time.sleep(0.25)
    return {
        "error": "Blender did not respond in time. Is Blender open with the Griptape Bridge "
        "add-on enabled and 'Listen for Commands' turned on?"
    }


def _critique_snapshot(job: dict, image_data_url: str, width: int, height: int) -> str:
    """Show the snapshot to a vision model and return its critique."""
    import base64

    from griptape.artifacts import ImageArtifact, ListArtifact, TextArtifact
    from griptape.common import PromptStack
    from griptape.drivers.prompt.griptape_cloud import GriptapeCloudPromptDriver

    try:
        from griptape_nodes.drivers.cloud_models import VISION_MODEL_CHOICES

        vision_models = list(VISION_MODEL_CHOICES)
    except Exception:
        vision_models = []

    model = job.get("model") or ""
    if model not in vision_models:
        model = _VIEW_FALLBACK_MODEL if (_VIEW_FALLBACK_MODEL in vision_models or not vision_models) else vision_models[0]

    image_bytes = base64.b64decode(image_data_url.split(",", 1)[-1])
    image = ImageArtifact(value=image_bytes, format="png", width=width, height=height)

    driver_kwargs: dict[str, Any] = {"model": model}
    if job.get("_api_key"):
        driver_kwargs["api_key"] = job["_api_key"]
    driver = GriptapeCloudPromptDriver(**driver_kwargs)

    stack = PromptStack()
    stack.add_user_message(ListArtifact([TextArtifact(_CRITIQUE_PROMPT.format(goal=job["prompt"])), image]))
    return driver.run(stack).to_text()


def _build_scene_tool(job: dict):
    """Build the BlenderSceneTool class lazily (griptape imports engine-side only)."""
    from griptape.artifacts import TextArtifact
    from griptape.tools import BaseTool
    from griptape.utils.decorators import activity
    from schema import Literal, Optional as SchemaOptional, Or, Schema

    def _call(command: str, params: dict) -> TextArtifact:
        result = _run_blender_command(command, params, job)
        return TextArtifact(json.dumps(result))

    # LLMs send ints and floats interchangeably; keep vectors permissive.
    _vec = list
    _num = Or(int, float)

    class BlenderSceneTool(BaseTool):
        @activity(
            config={
                "description": "List the Blender scene contents: objects with names, types, and locations, "
                "plus frame range and active camera.",
            }
        )
        def scene_info(self, _params: dict) -> TextArtifact:
            return _call("scene_info", {})

        @activity(
            config={
                "description": "Add a primitive to the scene. Kinds: cube, sphere, plane, cylinder, cone, "
                "torus, monkey, empty, camera, light_point, light_sun, light_area, light_spot. Lights are "
                "created with default settings; set their color and energy afterwards via run_python.",
                "schema": Schema(
                    {
                        Literal("kind", description="Primitive kind"): str,
                        SchemaOptional(Literal("name", description="Object name")): str,
                        SchemaOptional(Literal("location", description="[x, y, z] meters")): _vec,
                        SchemaOptional(Literal("rotation_euler", description="[x, y, z] radians")): _vec,
                        SchemaOptional(Literal("scale", description="[x, y, z] factors")): _vec,
                    }
                ),
            }
        )
        def add_primitive(self, params: dict) -> TextArtifact:
            return _call("add_primitive", params["values"])

        @activity(
            config={
                "description": "Move, rotate, or scale an existing object by name.",
                "schema": Schema(
                    {
                        Literal("name", description="Existing object name"): str,
                        SchemaOptional(Literal("location", description="[x, y, z] meters")): _vec,
                        SchemaOptional(Literal("rotation_euler", description="[x, y, z] radians")): _vec,
                        SchemaOptional(Literal("scale", description="[x, y, z] factors")): _vec,
                    }
                ),
            }
        )
        def move_object(self, params: dict) -> TextArtifact:
            return _call("move_object", params["values"])

        @activity(
            config={
                "description": "Delete an object from the scene by name.",
                "schema": Schema({Literal("name", description="Object name"): str}),
            }
        )
        def delete_object(self, params: dict) -> TextArtifact:
            return _call("delete_object", params["values"])

        @activity(
            config={
                "description": "Position the active camera and optionally set its focal length.",
                "schema": Schema(
                    {
                        SchemaOptional(Literal("location", description="[x, y, z] meters")): _vec,
                        SchemaOptional(Literal("rotation_euler", description="[x, y, z] radians")): _vec,
                        SchemaOptional(Literal("lens_mm", description="Focal length in mm")): _num,
                    }
                ),
            }
        )
        def set_camera(self, params: dict) -> TextArtifact:
            return _call("set_camera", params["values"])

        @activity(
            config={
                "description": "Render a snapshot of the scene through the active camera and receive an art "
                "director's critique of composition, lighting, materials, and placement. Use after building "
                "to find and fix visual problems. Requires an active camera.",
            }
        )
        def view_scene(self, _params: dict) -> TextArtifact:
            result = _run_blender_command("snapshot", {"width": 960, "height": 540}, job)
            data_url = result.get("image_data_url", "")
            if not data_url:
                return TextArtifact(json.dumps({"error": result.get("error", "snapshot failed - is there an active camera?")}))
            try:
                critique = _critique_snapshot(job, data_url, int(result.get("width", 960)), int(result.get("height", 540)))
            except Exception as exc:
                return TextArtifact(json.dumps({"error": f"snapshot ok but critique failed: {exc}"}))
            return TextArtifact(critique)

        @activity(
            config={
                "description": "Run a short Python script inside Blender using the bpy API, for anything "
                "the other tools cannot do (materials, modifiers, curves, lights settings...). "
                "Set a JSON-serializable variable named 'result' to report data back. "
                "Only works when the user enabled 'Allow Python Commands' in the add-on preferences.",
                "schema": Schema({Literal("code", description="Python source using bpy"): str}),
            }
        )
        def run_python(self, params: dict) -> TextArtifact:
            return _call("run_python", params["values"])

    return BlenderSceneTool()


def _job_worker(job_id: str) -> None:
    job = _jobs.get(job_id)
    if job is None:
        return
    try:
        from griptape.drivers.prompt.griptape_cloud import GriptapeCloudPromptDriver
        from griptape.structures import Agent

        api_key: Optional[str] = None
        try:
            from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

            api_key = GriptapeNodes.SecretsManager().get_secret("GT_CLOUD_API_KEY", should_error_on_not_found=False)
        except Exception:
            api_key = None
        job["_api_key"] = api_key  # reused by view_scene's vision critique

        from griptape.artifacts import ErrorArtifact

        driver_kwargs: dict[str, Any] = {}
        if job.get("model"):
            driver_kwargs["model"] = job["model"]
        if api_key:
            driver_kwargs["api_key"] = api_key

        def _run_agent(extra_params: Optional[dict] = None):
            kwargs = dict(driver_kwargs)
            if extra_params:
                kwargs["extra_params"] = extra_params
            agent = Agent(prompt_driver=GriptapeCloudPromptDriver(**kwargs), tools=[_build_scene_tool(job)])
            agent.run(_SYSTEM_INSTRUCTIONS + job["prompt"])
            return getattr(agent, "output", None)

        job["status"] = "running"
        output = _run_agent()

        # Reasoning-tuned models (gpt-5.4+) reject tool calls on the chat
        # endpoint with a bare 400 unless reasoning is disabled. The framework
        # swallows that into the output, so detect it and retry once -- but
        # only if no scene commands ran yet (a fresh agent redoes everything).
        output_text = str(getattr(output, "value", output) or "")
        if "400 Client Error" in output_text and not job["actions"]:
            output = _run_agent({"reasoning_effort": "none"})
            output_text = str(getattr(output, "value", output) or "")

        if job.get("cancel"):
            job["status"] = "cancelled"
        elif isinstance(output, ErrorArtifact):
            job["error"] = output_text
            job["status"] = "error"
        else:
            job["result"] = output_text
            job["status"] = "done"
    except Exception as exc:
        if job.get("cancel"):
            job["status"] = "cancelled"
        else:
            job["error"] = f"{exc}\n{traceback.format_exc(limit=3)}"
            job["status"] = "error"


def submit_agent_job(prompt: str, model: str = "") -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Empty prompt.")
    job_id = uuid.uuid4().hex
    with _lock:
        if len(_jobs) > MAX_JOBS_KEPT:
            finished = [jid for jid, j in _jobs.items() if j["status"] in ("done", "error")]
            for jid in finished[: len(_jobs) - MAX_JOBS_KEPT]:
                _jobs.pop(jid, None)
        _jobs[job_id] = {
            "id": job_id,
            "prompt": prompt,
            "model": (model or "").strip(),
            "status": "starting",
            "result": None,
            "error": None,
            "actions": [],
        }
    threading.Thread(target=_job_worker, args=(job_id,), name=f"gtn-scene-agent-{job_id[:8]}", daemon=True).start()
    return job_id


def cancel_agent_job(job_id: str) -> bool:
    """Flag a job as cancelled and flush any scene commands it left queued."""
    job = _jobs.get(job_id)
    if job is None:
        return False
    job["cancel"] = True
    if job["status"] in ("starting", "running"):
        job["status"] = "cancelled"
    try:
        _bridge().cancel_queued_commands()
    except Exception:
        pass
    return True


def get_agent_job(job_id: str) -> Optional[dict]:
    job = _jobs.get(job_id)
    if job is None:
        return None
    return {
        "id": job["id"],
        "status": job["status"],
        "result": job["result"],
        "error": job["error"],
        "actions": job["actions"],
    }
