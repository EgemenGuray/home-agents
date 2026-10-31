"""End-to-end user journeys (voice, approvals, outages), checked for friction rather than plumbing."""

import importlib.util
from pathlib import Path

import anthropic
import httpx
from conftest import FakeClient, block, response
from fastapi.testclient import TestClient

from home_agents.agent import VOICE_TIMEOUT_S, Agent, describe
from home_agents.api import CLOUD_DOWN, approval_push, build_app, followup
from home_agents.approvals import Approvals
from home_agents.audit import AuditLog
from home_agents.localllm import LocalModel
from home_agents.memory import Memory
from home_agents.models import Spend
from home_agents.settings import Settings
from home_agents.tools import Tool, ToolRegistry

AUTH = {"Authorization": "Bearer test-token"}


class FakeHA:
    def __init__(self):
        self.calls = []

    def call_service(self, inp):
        self.calls.append(inp)
        return {"ok": True}


def _agent(conn, client, approvals, tool_calls, followups):
    unlock = Tool("ha_call_service", "", {"type": "object"}, lambda i: tool_calls.append(i) or {"ok": True},
                  lambda i: i.get("domain") == "lock")
    return Agent(client, ToolRegistry([unlock]), Memory(conn), Spend(conn, 1.4, 5, 70), AuditLog(conn), approvals,
                 followup=lambda text, channel, room: followups.append((text, channel, room)))


def test_voice_approval_runs_on_one_signature_and_is_announced_in_the_room(conn, phone):
    """'Unlock the front door' from the kitchen: one push, Face ID, the door opens, the kitchen hears it."""
    pushes, tool_calls, followups = [], [], []
    approvals = Approvals([phone.public_pem], notify=pushes.append)
    client = FakeClient([
        response([block(type="tool_use", id="t1", name="ha_call_service",
                        input={"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"})], "tool_use"),
        response([block(type="text", text="I've asked for approval on your phone.")]),
    ])
    agent = _agent(conn, client, approvals, tool_calls, followups)
    reply = agent.converse("unlock the front door", channel="voice", room="kitchen")

    assert len(pushes) == 1 and pushes[0].summary == "unlock front door"  # readable on the lock screen
    assert tool_calls == []  # nothing happens before the signature
    req = approvals.get(reply.pending_approvals[0])
    approvals.decide(req.id, "approve", phone.sign(req, "approve"))
    assert tool_calls == [{"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"}]
    assert followups == [("Approved. Done: unlock front door.", "voice", "kitchen")]
    assert Memory(conn).history(reply.conversation_id)[-1]["content"].startswith("Approved. Done")


def test_denied_approval_runs_nothing(conn, phone):
    tool_calls, followups = [], []
    approvals = Approvals([phone.public_pem])
    client = FakeClient([
        response([block(type="tool_use", id="t1", name="ha_call_service",
                        input={"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"})], "tool_use"),
        response([block(type="text", text="Waiting for approval.")]),
    ])
    reply = _agent(conn, client, approvals, tool_calls, followups).converse("unlock", channel="voice", room="kitchen")
    req = approvals.get(reply.pending_approvals[0])
    approvals.decide(req.id, "deny", phone.sign(req, "deny"))
    assert tool_calls == [] and followups == []


def test_failing_follow_up_does_not_undo_the_decision(conn, phone):
    approvals = Approvals([phone.public_pem], notify=lambda r: (_ for _ in ()).throw(RuntimeError("push down")))
    req = approvals.request("tool", "x", {}, on_approve=lambda r: (_ for _ in ()).throw(RuntimeError("HA down")))
    assert approvals.decide(req.id, "approve", phone.sign(req, "approve")).state == "approved"


def test_voice_calls_have_a_short_timeout(conn):
    client = FakeClient([response([block(type="text", text="It's 4 degrees.")])])
    _agent(conn, client, None, [], []).converse("weather?", channel="voice")
    assert client.requests[0]["timeout"] == VOICE_TIMEOUT_S
    client = FakeClient([response([block(type="text", text="Here's a long answer.")])])
    _agent(conn, client, None, [], []).converse("explain", channel="app")
    assert "timeout" not in client.requests[0]


def test_cloud_down_says_something_useful_instead_of_a_500(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_AGENTS_SERVICE_TOKEN", "test-token")

    class Down(FakeClient):
        def _create(self, **kwargs):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.invalid"))

    app = build_app(Settings(data_dir=tmp_path, policy_file=tmp_path / "none.yaml", harness_file=tmp_path / "none.yaml"),
                    Down([]), start_workers=False)
    r = TestClient(app).post("/v1/converse", json={"text": "what's the weather"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["text"] == CLOUD_DOWN and r.json()["degraded"]


def test_studio_local_model_is_wired_in_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_AGENTS_SERVICE_TOKEN", "test-token")
    app = build_app(Settings(data_dir=tmp_path, policy_file=tmp_path / "none.yaml", harness_file=tmp_path / "none.yaml",
                             local_llm_url="http://studio.invalid:8080"), FakeClient([]), start_workers=False)
    assert isinstance(app.state.agent.local, LocalModel)


def test_switched_off_studio_is_probed_at_most_every_30_seconds():
    probes = []

    def handler(request):
        probes.append(request.url.path)
        raise httpx.ConnectError("off")

    now = [0.0]
    local = LocalModel("http://studio.invalid", client=httpx.Client(transport=httpx.MockTransport(handler)),
                       clock=lambda: now[0])
    assert not local.available() and not local.available()
    assert len(probes) == 1
    now[0] = 31
    local.available()
    assert len(probes) == 2


def test_approval_push_deep_links_to_the_request():
    ha = FakeHA()
    approvals = Approvals([], notify=approval_push(ha, {"app_link": "homeapp://approvals"}))
    req = approvals.request("tool", "unlock front door", {"x": 1})
    call = ha.calls[0]
    assert (call["domain"], call["service"]) == ("notify", "owner_phone")
    assert call["data"]["data"]["url"] == f"homeapp://approvals/{req.id}"
    assert "unlock front door" in call["data"]["message"] and "Expires in 5 min" in call["data"]["message"]


def test_follow_up_speaks_in_the_room_for_voice_and_pushes_for_typed_requests():
    ha = FakeHA()
    send = followup(ha, "notify.owner_phone")
    send("Done.", "voice", "kitchen")
    send("Done.", "app", None)
    assert ha.calls[0] == {"domain": "script", "service": "announce", "data": {"message": "Done.", "rooms": ["kitchen"]}}
    assert ha.calls[1]["domain"] == "notify"


def test_describe_is_human():
    assert describe("ha_call_service", {"domain": "cover", "service": "open_cover", "entity_id": "cover.garage_door"}) \
        == "open cover garage door"
    assert describe("send_email", {"to": "x"}) == "send email (to=x)"


def _logic():
    path = Path(__file__).parents[1] / "ha_integration/custom_components/home_agent/logic.py"
    spec = importlib.util.spec_from_file_location("home_agent_logic", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ha_integration_decisions():
    logic = _logic()
    assert logic.local_handled("action_done", None)          # "turn off the kitchen lights": HA, no cloud
    assert not logic.local_handled("error", "no_intent_match")  # "what's the weather": the agent
    assert not logic.local_handled("error", "no_valid_targets")  # unknown name: the agent can resolve it
    assert logic.keep_listening("Which room? ") and not logic.keep_listening("Done.")
    assert logic.timeout_for("voice") < logic.timeout_for("app")
