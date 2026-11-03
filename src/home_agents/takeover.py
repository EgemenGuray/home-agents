"""Screen share and take over (S1.6.1): watch an agent's desktop from the app and take the wheel.

Modelled on how a person shares one Mac with an agent: the agent drives while you watch, a glow
shows it is driving, touching the screen pauses it and gives you control, and Hand back lets it
carry on with a note of what you did. The desktop has one pointer and one keyboard, so exactly one
side drives at a time:

    agent  --take / touch-->  owner  --hand back-->  agent
      |                         |
      +--pause-->  paused  <----+ (pause, or you stop touching for `idle_minutes`)
                     +--------hand back-------->  agent

The agent's task is paused between steps while you drive (its current step finishes, the next one
waits), and the screen relay (`screen_relay.py`) drops your input unless you hold a control ticket.
Tickets are short-lived and signed with a key shared only by this service and the relay, so the
relay never needs the service token and a leaked view link can't drive anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

AGENT, OWNER, PAUSED = "agent", "owner", "paused"
VIEW, CONTROL = "view", "control"


class TakeoverError(Exception):
    pass


class TicketError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_ticket(key: bytes, workspace: str, mode: str, expires_at: float) -> str:
    body = _b64(json.dumps({"ws": workspace, "mode": mode, "exp": int(expires_at), "n": secrets.token_hex(8)},
                           separators=(",", ":"), sort_keys=True).encode())
    mac = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


class TicketVerifier:
    """Used by the relay. Each ticket opens one connection or upgrades one connection, once."""

    def __init__(self, key: bytes, clock: Callable[[], float] = time.time):
        if len(key) < 32:
            raise ValueError("screen key must be at least 32 bytes")
        self.key = key
        self.clock = clock
        self._used: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, ticket: str, workspace: str) -> tuple[str, float]:
        try:
            body, mac = ticket.split(".", 1)
            want = _b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(mac, want):
                raise TicketError("bad signature")
            claims = json.loads(_unb64(body))
        except TicketError:
            raise
        except Exception as exc:
            raise TicketError("malformed ticket") from exc
        now = self.clock()
        if claims.get("ws") != workspace:
            raise TicketError("ticket is for another workspace")
        if claims.get("mode") not in (VIEW, CONTROL):
            raise TicketError("unknown mode")
        if claims["exp"] <= now:
            raise TicketError("ticket expired")
        with self._lock:
            self._used = {n: exp for n, exp in self._used.items() if exp > now}
            if claims["n"] in self._used:
                raise TicketError("ticket already used")
            self._used[claims["n"]] = claims["exp"]
        return claims["mode"], float(claims["exp"])


@dataclass
class Screen:
    workspace: str
    job_id: str | None
    state: str = AGENT
    since: float = 0.0
    owner_seen: float = 0.0
    took_at: float = 0.0
    log: list[str] = field(default_factory=list)


def _duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    return f"{s // 60} min {s % 60} s" if s % 60 else f"{s // 60} min"


class Takeover:
    """One record per desktop workspace (Linux desktop or macOS VM) and who is driving it."""

    def __init__(self, key: bytes, *, pause_job: Callable[[str], object] = lambda job: None,
                 resume_job: Callable[[str], object] = lambda job: None,
                 steer_job: Callable[[str, str], object] = lambda job, text: None,
                 describe_job: Callable[[str], dict] = lambda job: {},
                 audit=None, notify: Callable[[str, str], None] = lambda title, text: None,
                 clock: Callable[[], float] = time.time, ticket_seconds: int = 60, idle_minutes: int = 10):
        if len(key) < 32:
            raise ValueError("screen key must be at least 32 bytes")
        self.key = key
        self.pause_job, self.resume_job, self.steer_job = pause_job, resume_job, steer_job
        self.describe_job = describe_job
        self.audit = audit
        self.notify = notify
        self.clock = clock
        self.ticket_seconds = ticket_seconds
        self.idle_seconds = idle_minutes * 60
        self._screens: dict[str, Screen] = {}
        self._lock = threading.RLock()

    # Workspaces come and go --------------------------------------------------------------------------

    def attach(self, workspace: str, job_id: str | None) -> None:
        with self._lock:
            self._screens[workspace] = Screen(workspace, job_id, since=self.clock())

    def detach(self, workspace: str) -> None:
        with self._lock:
            screen = self._screens.pop(workspace, None)
        if screen and screen.state == OWNER:
            self._record("takeover_ended", screen, {"seconds": round(self.clock() - screen.took_at), "reason": "workspace closed"})

    def _get(self, workspace: str) -> Screen:
        screen = self._screens.get(workspace)
        if screen is None:
            raise TakeoverError(f"no screen for {workspace}")
        return screen

    # What the app shows -------------------------------------------------------------------------------

    def status(self, workspace: str) -> dict:
        with self._lock:
            s = self._get(workspace)
            job = self.describe_job(s.job_id) if s.job_id else {}
            return {
                "workspace": s.workspace, "job_id": s.job_id, "title": job.get("title", ""),
                "state": s.state,
                "driver": {AGENT: "agent", OWNER: "you", PAUSED: "nobody"}[s.state],
                "glow": s.state == AGENT,          # the edge glows while the agent has the pointer
                "recent_actions": job.get("recent_actions", [])[-3:],
                "seconds_in_state": round(self.clock() - s.since),
                "your_actions": list(s.log[-5:]),
            }

    def screens(self) -> list[dict]:
        return [self.status(w) for w in list(self._screens)]

    def ticket(self, workspace: str, mode: str = VIEW) -> dict:
        """A view ticket any time; a control ticket only while you hold the pointer."""
        with self._lock:
            s = self._get(workspace)
            if mode == CONTROL and s.state != OWNER:
                raise TakeoverError("take over first: the agent or nobody is driving")
            if mode not in (VIEW, CONTROL):
                raise TakeoverError("mode is view or control")
            if mode == CONTROL:
                s.owner_seen = self.clock()
        exp = self.clock() + self.ticket_seconds
        return {"ticket": sign_ticket(self.key, workspace, mode, exp), "mode": mode, "expires_in": self.ticket_seconds}

    # Who drives ---------------------------------------------------------------------------------------

    def take(self, workspace: str, by: str = "owner") -> dict:
        """Touching the screen or pressing Take over: the agent stops before its next step, you drive."""
        with self._lock:
            s = self._get(workspace)
            now = self.clock()
            if s.state != OWNER:
                if s.state == AGENT and s.job_id:
                    self.pause_job(s.job_id)
                s.state, s.since, s.took_at, s.owner_seen, s.log = OWNER, now, now, now, []
                self._record("takeover_started", s, {"by": by})
        return self.ticket(workspace, CONTROL)

    def pause(self, workspace: str, by: str = "owner") -> dict:
        """Nobody drives: the agent waits and your input is ignored. From the banner or the Lock Screen."""
        with self._lock:
            s = self._get(workspace)
            if s.state == AGENT and s.job_id:
                self.pause_job(s.job_id)
            if s.state != PAUSED:
                if s.state == OWNER:
                    self._record("takeover_ended", s, {"seconds": round(self.clock() - s.took_at), "reason": "paused"})
                s.state, s.since = PAUSED, self.clock()
                self._record("screen_paused", s, {"by": by})
        return self.status(workspace)

    def hand_back(self, workspace: str, note: str = "", by: str = "owner") -> dict:
        """Resume the agent, telling it what changed so it looks again before acting."""
        with self._lock:
            s = self._get(workspace)
            if s.state == AGENT:
                return self.status(workspace)
            seconds = self.clock() - s.took_at if s.took_at else 0
            did = "; ".join(x for x in (note.strip(), *s.log) if x)
            if s.job_id:
                if s.took_at:
                    msg = f"The owner took over your screen for {_duration(seconds)}"
                    msg += f" and did: {did}." if did else "."
                else:
                    msg = "The owner paused you."
                msg += " The screen may have changed: look at it again before your next action, and don't redo what they did."
                self.steer_job(s.job_id, msg)
                self.resume_job(s.job_id)
            if s.took_at:
                self._record("takeover_ended", s, {"seconds": round(seconds), "note": note, "reason": "handed back", "by": by})
            s.state, s.since, s.took_at, s.log = AGENT, self.clock(), 0.0, []
        return self.status(workspace)

    def note(self, workspace: str, text: str) -> None:
        """What you did while driving (typed in the banner), passed to the agent on hand back."""
        with self._lock:
            s = self._get(workspace)
            if s.state == OWNER and text.strip():
                s.log.append(text.strip()[:200])
                s.owner_seen = self.clock()

    def sweep(self) -> list[str]:
        """Run every minute: if you took over and walked away, pause rather than leave it half-driven."""
        now = self.clock()
        left = []
        with self._lock:
            for s in self._screens.values():
                if s.state == OWNER and now - s.owner_seen > self.idle_seconds:
                    self._record("takeover_ended", s, {"seconds": round(now - s.took_at), "reason": "idle"})
                    s.state, s.since = PAUSED, now
                    left.append(s.workspace)
        for w in left:
            title = (self.describe_job(self._screens[w].job_id) or {}).get("title", w) if self._screens[w].job_id else w
            self.notify("Agent paused", f"You stopped driving {title}. It is paused; open the screen to hand it back.")
        return left

    def _record(self, kind: str, s: Screen, detail: dict) -> None:
        if self.audit is not None:
            self.audit.record(kind, "owner", {"workspace": s.workspace, **detail}, s.job_id)
