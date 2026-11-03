"""HTTP routes for screen share and take over (S1.6.1), on the agent service.

The app (and the web viewer at /screen) asks here for tickets and who drives; the pixels go
straight from the screen relay on the workspace host to the app over the tailnet. Every route needs
the service token. Take over needs no phone signature: it only ever stops the agent, it never lets
the agent do more.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .takeover import CONTROL, VIEW, Takeover, TakeoverError

VIEWER = Path(__file__).parent / "harness" / "console" / "screen.html"
# noVNC's ES modules, vendored into the image at a pinned commit (Dockerfile) and served from here, so the
# page that holds the service token loads no third-party script.
NOVNC_DIR = Path(os.environ.get("NOVNC_DIR", "/opt/novnc"))


class NoteIn(BaseModel):
    text: str = ""


class TicketIn(BaseModel):
    mode: str = VIEW


def screen_key_from_env() -> bytes | None:
    raw = os.environ.get("HOME_AGENTS_SCREEN_KEY", "")
    return bytes.fromhex(raw) if raw else None


def harness_hooks(harness) -> dict:
    """Wire take over to harness tasks: pause between steps, steer with what you did, resume."""
    if harness is None:
        return {}

    def describe(job_id: str) -> dict:
        t = harness.store.task(job_id)
        if not t:
            return {}
        tools = [e for e in harness.store.events(job_id) if e["kind"] == "tool"]
        return {"title": t["title"], "recent_actions": [e["data"].get("name", "") for e in tools[-3:]]}

    return {"pause_job": harness.tasks.pause, "resume_job": harness.tasks.resume,
            "steer_job": harness.tasks.steer, "describe_job": describe}


def mount(app: FastAPI, screens: Takeover, auth, relay_url: str = "", novnc_dir: Path = NOVNC_DIR) -> None:
    deps = [Depends(auth)]
    if novnc_dir.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/screen/novnc", StaticFiles(directory=novnc_dir), name="novnc")

    def guard(fn, *args):
        try:
            return fn(*args)
        except TakeoverError as exc:
            raise HTTPException(409 if "first" in str(exc) else 404, str(exc)) from exc

    def with_url(workspace: str, ticket: dict) -> dict:
        return {**ticket, "url": f"{relay_url.rstrip('/')}/screen/{workspace}" if relay_url else ""}

    @app.get("/screen", response_class=HTMLResponse)
    def viewer():
        return HTMLResponse(VIEWER.read_text(), headers={"Content-Security-Policy":
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' wss:; img-src 'self' data:; "
            "frame-ancestors 'none'"})

    @app.get("/v1/screens", dependencies=deps)
    def list_screens():
        return screens.screens()

    @app.get("/v1/screens/{workspace}", dependencies=deps)
    def status(workspace: str):
        return guard(screens.status, workspace)

    @app.post("/v1/screens/{workspace}/ticket", dependencies=deps)
    def ticket(workspace: str, body: TicketIn):
        if body.mode not in (VIEW, CONTROL):
            raise HTTPException(400, "mode is view or control")
        return with_url(workspace, guard(screens.ticket, workspace, body.mode))

    @app.post("/v1/screens/{workspace}/take", dependencies=deps)
    def take(workspace: str):
        return with_url(workspace, guard(screens.take, workspace))

    @app.post("/v1/screens/{workspace}/pause", dependencies=deps)
    def pause(workspace: str):
        return guard(screens.pause, workspace)

    @app.post("/v1/screens/{workspace}/note", dependencies=deps)
    def note(workspace: str, body: NoteIn):
        guard(screens.note, workspace, body.text)
        return {"ok": True}

    @app.post("/v1/screens/{workspace}/hand-back", dependencies=deps)
    def hand_back(workspace: str, body: NoteIn):
        return guard(screens.hand_back, workspace, body.text)


def start_sweeper(screens: Takeover, every_seconds: float = 60) -> threading.Event:  # pragma: no cover
    stop = threading.Event()

    def loop():
        while not stop.wait(every_seconds):
            screens.sweep()
    threading.Thread(target=loop, daemon=True, name="screen-sweeper").start()
    return stop
