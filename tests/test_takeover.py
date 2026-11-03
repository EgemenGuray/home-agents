"""Screen share and take over (S1.6.1): tickets, who drives, the relay's input filter, the API."""

import json
import struct

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import FakeClient
from home_agents.api import build_app
from home_agents.audit import AuditLog
from home_agents.screen_relay import ProtocolError, RfbInputFilter, build_relay
from home_agents.settings import Settings
from home_agents.takeover import (AGENT, OWNER, PAUSED, Takeover, TakeoverError, TicketError, TicketVerifier,
                                  sign_ticket)
from home_agents.workspaces import DockerWorkspaces, WorkspaceRouter

KEY = bytes(range(32))
WS = "ws-0123abcdef"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Job:
    """Records what the takeover controller does to the agent's task."""

    def __init__(self):
        self.calls = []

    def hooks(self):
        return {"pause_job": lambda j: self.calls.append(("pause", j)),
                "resume_job": lambda j: self.calls.append(("resume", j)),
                "steer_job": lambda j, t: self.calls.append(("steer", j, t)),
                "describe_job": lambda j: {"title": "TTC reimbursement", "recent_actions": ["a", "b", "c", "click Download"]}}


def controller(conn=None, clock=None, **kw):
    job = Job()
    pushes = []
    t = Takeover(KEY, audit=AuditLog(conn) if conn else None, clock=clock or Clock(),
                 notify=lambda title, text: pushes.append((title, text)), **job.hooks(), **kw)
    t.attach(WS, "task_1")
    return t, job, pushes


# Tickets ----------------------------------------------------------------------------------------------

def test_ticket_round_trip_and_single_use():
    clock = Clock()
    v = TicketVerifier(KEY, clock)
    t = sign_ticket(KEY, WS, "view", clock() + 60)
    assert v.verify(t, WS) == ("view", clock() + 60)
    with pytest.raises(TicketError, match="already used"):
        v.verify(t, WS)


@pytest.mark.parametrize("make, msg", [
    (lambda c: sign_ticket(KEY, "ws-other00001", "control", c() + 60), "another workspace"),
    (lambda c: sign_ticket(KEY, WS, "control", c() - 1), "expired"),
    (lambda c: sign_ticket(b"x" * 32, WS, "control", c() + 60), "bad signature"),
    (lambda c: sign_ticket(KEY, WS, "admin", c() + 60), "unknown mode"),
    (lambda c: "not-a-ticket", "malformed"),
])
def test_bad_tickets_are_refused(make, msg):
    clock = Clock()
    with pytest.raises(TicketError, match=msg):
        TicketVerifier(KEY, clock).verify(make(clock), WS)


def test_tampered_ticket_is_refused():
    clock = Clock()
    body, mac = sign_ticket(KEY, WS, "view", clock() + 60).split(".")
    forged = sign_ticket(KEY, WS, "control", clock() + 60).split(".")[0]
    with pytest.raises(TicketError):
        TicketVerifier(KEY, clock).verify(f"{forged}.{mac}", WS)


def test_short_keys_are_refused():
    with pytest.raises(ValueError):
        TicketVerifier(b"short")
    with pytest.raises(ValueError):
        Takeover(b"short")


# Who drives -------------------------------------------------------------------------------------------

def test_agent_drives_and_glows_until_you_take_over(conn):
    t, job, _ = controller(conn)
    s = t.status(WS)
    assert (s["state"], s["driver"], s["glow"]) == (AGENT, "agent", True)
    assert s["title"] == "TTC reimbursement" and s["recent_actions"] == ["b", "c", "click Download"]
    with pytest.raises(TakeoverError, match="take over first"):
        t.ticket(WS, "control")

    ticket = t.take(WS)
    assert ticket["mode"] == "control" and TicketVerifier(KEY, t.clock).verify(ticket["ticket"], WS)[0] == "control"
    assert job.calls == [("pause", "task_1")]
    s = t.status(WS)
    assert (s["state"], s["driver"], s["glow"]) == (OWNER, "you", False)
    assert AuditLog(conn).search(kind="takeover_started")


def test_taking_over_twice_does_not_pause_twice():
    t, job, _ = controller()
    t.take(WS)
    t.take(WS)
    assert job.calls == [("pause", "task_1")]


def test_hand_back_tells_the_agent_what_you_did(conn):
    clock = Clock()
    t, job, _ = controller(conn, clock)
    t.take(WS)
    t.note(WS, "solved the CAPTCHA")
    clock.t += 125
    s = t.hand_back(WS, "logged in")
    assert s["state"] == AGENT and s["glow"]
    kind, task, text = job.calls[1]
    assert (kind, task) == ("steer", "task_1")
    assert "2 min 5 s" in text and "logged in; solved the CAPTCHA" in text and "look at it again" in text
    assert job.calls[2] == ("resume", "task_1")
    ended = AuditLog(conn).search(kind="takeover_ended")
    assert ended and json.loads(ended[0]["detail"])["seconds"] == 125
    with pytest.raises(TakeoverError):
        t.ticket(WS, "control")      # control is gone once you hand back


def test_pause_stops_everyone_and_hand_back_resumes():
    t, job, _ = controller()
    s = t.pause(WS)
    assert (s["state"], s["driver"], s["glow"]) == (PAUSED, "nobody", False)
    assert job.calls == [("pause", "task_1")]
    with pytest.raises(TakeoverError):
        t.ticket(WS, "control")
    t.hand_back(WS)
    assert job.calls[1][0] == "steer" and "paused you" in job.calls[1][2]
    assert job.calls[2] == ("resume", "task_1")


def test_hand_back_while_agent_drives_is_a_no_op():
    t, job, _ = controller()
    t.hand_back(WS)
    assert job.calls == []


def test_walking_away_pauses_and_pushes(conn):
    clock = Clock()
    t, job, pushes = controller(conn, clock, idle_minutes=10)
    t.take(WS)
    clock.t += 9 * 60
    t.ticket(WS, "control")          # the app renewing control counts as you still being there
    clock.t += 9 * 60
    assert t.sweep() == []
    clock.t += 2 * 60
    assert t.sweep() == [WS]
    assert t.status(WS)["state"] == PAUSED
    assert pushes and "TTC reimbursement" in pushes[0][1]
    assert ("resume", "task_1") not in job.calls   # it never hands itself back to the agent


def test_closing_the_workspace_ends_the_takeover(conn):
    t, _, _ = controller(conn)
    t.take(WS)
    t.detach(WS)
    assert json.loads(AuditLog(conn).search(kind="takeover_ended")[0]["detail"])["reason"] == "workspace closed"
    with pytest.raises(TakeoverError):
        t.status(WS)


def test_router_registers_desktops_only():
    docker = _FakeDocker()
    t = Takeover(KEY)
    router = WorkspaceRouter(DockerWorkspaces({"home": 18080}, docker), screens=t)
    desk = router.create(job_id="task_9", kind="desktop", route="home")
    cli = router.create(job_id="task_9", kind="cli", route="home")
    assert [s["workspace"] for s in t.screens()] == [desk]
    # The desktop's VNC socket lives in its own volume; the shell gets none.
    assert docker.runs[0][1]["volumes"] == {f"screen-{desk}": {"bind": "/run/screen", "mode": "rw"}}
    assert docker.runs[1][1]["volumes"] is None
    router.destroy(desk)
    assert t.screens() == [] and docker.volumes_removed == [f"screen-{desk}"]
    router.destroy(cli)


class _FakeDocker:
    def __init__(self):
        self.runs, self.volumes_removed = [], []
        parent = self

        class Containers:
            def run(self, image, **kw):
                parent.runs.append((image, kw))

            def get(self, name):
                class C:
                    def remove(self, force):
                        pass
                return C()

        class Volumes:
            def get(self, name):
                class V:
                    def remove(self, force):
                        parent.volumes_removed.append(name)
                return V()

        self.containers, self.volumes = Containers(), Volumes()


# The relay's input filter ------------------------------------------------------------------------------

HANDSHAKE = b"RFB 003.008\n" + b"\x01" + b"\x01"
FB_REQUEST = struct.pack(">BBHHHH", 3, 1, 0, 0, 1280, 800)
KEY_EVENT = struct.pack(">BBxxI", 4, 1, 0x61)
POINTER = struct.pack(">BBHH", 5, 1, 10, 20)
ENCODINGS = struct.pack(">BxH", 2, 2) + struct.pack(">ii", 0, -223)
CUT = struct.pack(">BxxxI", 6, 3) + b"abc"


def test_view_only_drops_input_and_keeps_the_picture():
    f = RfbInputFilter()
    out, dropped = f.feed(HANDSHAKE + ENCODINGS + FB_REQUEST + KEY_EVENT + POINTER + CUT, allow_input=False)
    assert out == HANDSHAKE + ENCODINGS + FB_REQUEST and dropped == 3


def test_control_passes_input():
    f = RfbInputFilter()
    data = HANDSHAKE + KEY_EVENT + POINTER + CUT
    assert f.feed(data, allow_input=True) == (data, 0)


def test_messages_split_across_frames():
    f = RfbInputFilter()
    stream = HANDSHAKE + ENCODINGS + KEY_EVENT + FB_REQUEST
    out = b""
    for i in range(len(stream)):
        chunk, _ = f.feed(stream[i:i + 1], allow_input=False)
        out += chunk
    assert out == HANDSHAKE + ENCODINGS + FB_REQUEST


def test_unknown_or_huge_messages_are_refused():
    f = RfbInputFilter(handshake_bytes=0)
    with pytest.raises(ProtocolError):
        f.feed(b"\x63" + b"\x00" * 10, allow_input=True)
    with pytest.raises(ProtocolError):
        RfbInputFilter(handshake_bytes=0).feed(struct.pack(">BxxxI", 6, 50_000_000), allow_input=True)


# The relay -------------------------------------------------------------------------------------------

class FakeUpstream:
    """Stands in for the desktop's VNC socket: sends the server greeting, records what reaches it."""

    def __init__(self):
        self.received = bytearray()

    async def __call__(self, workspace):
        import asyncio
        received = self.received

        class Reader:
            sent = False

            async def read(self, n):
                if not self.sent:
                    self.sent = True
                    return b"RFB 003.008\n"
                await asyncio.sleep(3600)

        class Writer:
            def write(self, data):
                received.extend(data)

            async def drain(self):
                pass

            def close(self):
                pass
        return Reader(), Writer()


def _relay(clock):
    up = FakeUpstream()
    return TestClient(build_relay(TicketVerifier(KEY, clock), up, clock=clock)), up


def test_relay_view_ticket_shows_the_screen_but_drops_input():
    clock = Clock()
    http, up = _relay(clock)
    t = sign_ticket(KEY, WS, "view", clock() + 60)
    with http.websocket_connect(f"/screen/{WS}?ticket={t}&notices=1", subprotocols=["binary"]) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(HANDSHAKE + FB_REQUEST + KEY_EVENT)
        assert json.loads(ws.receive_text())["input"] == "dropped"
    assert bytes(up.received) == HANDSHAKE + FB_REQUEST


def test_relay_upgrades_to_control_in_place():
    clock = Clock()
    http, up = _relay(clock)
    view = sign_ticket(KEY, WS, "view", clock() + 60)
    with http.websocket_connect(f"/screen/{WS}?ticket={view}&notices=1") as ws:
        ws.receive_bytes()
        ws.send_text(json.dumps({"ticket": sign_ticket(KEY, WS, "control", clock() + 60)}))
        assert json.loads(ws.receive_text())["control"] is True
        ws.send_bytes(HANDSHAKE + KEY_EVENT)
        clock.t += 61                                    # control lapses when renewals stop
        ws.send_bytes(POINTER)
        assert json.loads(ws.receive_text())["input"] == "dropped"
    assert bytes(up.received) == HANDSHAKE + KEY_EVENT


def test_relay_sends_no_text_frames_to_novnc():
    clock = Clock()
    http, up = _relay(clock)
    t = sign_ticket(KEY, WS, "view", clock() + 60)
    with http.websocket_connect(f"/screen/{WS}?ticket={t}") as ws:
        ws.receive_bytes()
        ws.send_text(json.dumps({"ticket": "forged"}))
        ws.send_bytes(HANDSHAKE + KEY_EVENT + FB_REQUEST)
    assert bytes(up.received) == HANDSHAKE + FB_REQUEST


@pytest.mark.parametrize("path", [f"/screen/{WS}?ticket=bad", "/screen/..%2Fetc?ticket=x", "/screen/ws-UPPER?ticket=x"])
def test_relay_refuses_bad_tickets_and_names(path):
    http, _ = _relay(Clock())
    with pytest.raises(WebSocketDisconnect):
        with http.websocket_connect(path) as ws:
            ws.receive_bytes()


# The agent service's routes -------------------------------------------------------------------------

def _service(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_AGENTS_SERVICE_TOKEN", "test-token")
    monkeypatch.setenv("SCREEN_RELAY_URL", "wss://relay.example.ts.net:8710")
    t, job, _ = controller()
    app = build_app(Settings(data_dir=tmp_path, policy_file=tmp_path / "none.yaml"), FakeClient([]), screens=t)
    return TestClient(app), job


def test_api_take_over_and_hand_back(tmp_path, monkeypatch):
    http, job = _service(tmp_path, monkeypatch)
    h = {"Authorization": "Bearer test-token"}
    assert http.get("/v1/screens").status_code == 401
    assert http.get("/v1/screens", headers=h).json()[0]["driver"] == "agent"
    assert http.post(f"/v1/screens/{WS}/ticket", json={"mode": "control"}, headers=h).status_code == 409
    view = http.post(f"/v1/screens/{WS}/ticket", json={"mode": "view"}, headers=h).json()
    assert view["url"] == f"wss://relay.example.ts.net:8710/screen/{WS}" and view["mode"] == "view"

    took = http.post(f"/v1/screens/{WS}/take", headers=h).json()
    assert took["mode"] == "control" and took["url"].endswith(WS)
    assert http.post(f"/v1/screens/{WS}/note", json={"text": "typed the code"}, headers=h).json() == {"ok": True}
    back = http.post(f"/v1/screens/{WS}/hand-back", json={"text": ""}, headers=h).json()
    assert back["driver"] == "agent" and "typed the code" in job.calls[1][2]
    assert http.post(f"/v1/screens/{WS}/pause", headers=h).json()["driver"] == "nobody"
    assert http.get("/v1/screens/ws-nope000000", headers=h).status_code == 404
    assert http.post(f"/v1/screens/{WS}/ticket", json={"mode": "admin"}, headers=h).status_code == 400


def test_web_viewer_loads_no_third_party_script(tmp_path, monkeypatch):
    http, _ = _service(tmp_path, monkeypatch)
    r = http.get("/screen")
    assert r.status_code == 200 and "Take over" in r.text and "/screen/novnc/core/rfb.js" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline';" in csp and "frame-ancestors 'none'" in csp


def test_screens_stay_off_without_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_AGENTS_SERVICE_TOKEN", "test-token")
    monkeypatch.delenv("HOME_AGENTS_SCREEN_KEY", raising=False)
    app = build_app(Settings(data_dir=tmp_path, policy_file=tmp_path / "none.yaml"), FakeClient([]))
    assert app.state.screens is None
    assert TestClient(app).get("/v1/screens", headers={"Authorization": "Bearer test-token"}).status_code == 404


def test_key_in_env_turns_screens_on(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_AGENTS_SERVICE_TOKEN", "test-token")
    monkeypatch.setenv("HOME_AGENTS_SCREEN_KEY", KEY.hex())
    app = build_app(Settings(data_dir=tmp_path, policy_file=tmp_path / "none.yaml"), FakeClient([]), start_workers=False)
    assert isinstance(app.state.screens, Takeover)
