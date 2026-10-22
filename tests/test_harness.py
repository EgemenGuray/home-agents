import base64
import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from home_agents.approvals import Approvals
from home_agents.audit import AuditLog
from home_agents.harness.channels import Gateway, Pairing
from home_agents.harness.heartbeat import Heartbeat
from home_agents.harness.loop import HarnessAgent, Hooks
from home_agents.harness.mcp_tools import wrap_server
from home_agents.harness.profiles import Profile, load_profiles
from home_agents.harness.providers import (ChatResult, OpenAICompatProvider, ProviderDown, ToolCall, to_anthropic,
                                           to_openai)
from home_agents.harness.router import ModelRouter, NoModelAvailable
from home_agents.harness.skills import SkillLibrary
from home_agents.harness.store import Store
from home_agents.harness.tasks import TaskRunner
from home_agents.models import Spend
from home_agents.tools import Tool, ToolRegistry


class Scripted:
    """A provider that replays ChatResults and records what it was sent."""

    def __init__(self, name, results, location="cloud", up=True):
        self.name, self.location, self.up = name, location, up
        self.results = list(results)
        self.calls = []

    def available(self):
        return self.up

    def chat(self, model, system, messages, tools, max_tokens):
        self.calls.append({"model": model, "system": system, "messages": [dict(m) for m in messages], "tools": tools})
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        r.model = f"{self.name}:{model}"
        return r


def say(text, inp=10, out=5):
    return ChatResult(text, [], "end", inp, out)


def call(name, args, cid="c1"):
    return ChatResult("", [ToolCall(cid, name, args)], "tool_use", 10, 5)


def echo_tool(name="echo", approval=False):
    return Tool(name, "echo", {"type": "object", "properties": {"x": {"type": "string"}}}, lambda i: {"echo": i.get("x")},
                (lambda _i: True) if approval else (lambda _i: False))


@pytest.fixture
def spend(conn):
    return Spend(conn, usd_to_cad=1.4, daily_alert_cad=5, monthly_cap_cad=70)


def router_with(providers, chains, spend=None, conn=None):
    return ModelRouter({p.name: p for p in providers}, chains, spend, AuditLog(conn) if conn else None)


# Providers ---------------------------------------------------------------------------------------

def test_message_conversion_round_trip():
    msgs = [{"role": "user", "content": "hi", "images": [{"media_type": "image/png", "data": "AAA"}]},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "echo", "input": {"x": "1"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"},
            {"role": "tool", "tool_call_id": "t2", "content": "bad", "is_error": True}]
    a = to_anthropic(msgs)
    assert a[0]["content"][0]["type"] == "image" and a[1]["content"][0]["type"] == "tool_use"
    assert len(a) == 3 and [b["tool_use_id"] for b in a[2]["content"]] == ["t1", "t2"] and a[2]["content"][1]["is_error"]
    o = to_openai(msgs)
    assert o[0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert json.loads(o[1]["tool_calls"][0]["function"]["arguments"]) == {"x": "1"}
    assert o[2] == {"role": "tool", "tool_call_id": "t1", "content": "ok"}


def test_openai_compatible_provider_parses_tool_calls_and_fails_over_on_5xx(monkeypatch):
    replies = [httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
                   {"id": "x", "type": "function", "function": {"name": "echo", "arguments": "{\"x\": \"hi\"}"}}]}}],
                   "usage": {"prompt_tokens": 7, "completion_tokens": 3}}),
               httpx.Response(503, json={})]
    seen = []

    def handler(request):
        seen.append(request)
        return replies.pop(0)

    monkeypatch.setenv("OPENAI_TEST_KEY", "k")
    p = OpenAICompatProvider("openai", "https://api.example/v1", "OPENAI_TEST_KEY", client=httpx.Client(transport=httpx.MockTransport(handler)))
    r = p.chat("m", "sys", [{"role": "user", "content": "hi"}], [echo_tool().definition()], 100)
    assert r.stop == "tool_use" and r.tool_calls[0].input == {"x": "hi"} and r.model == "openai:m" and r.input_tokens == 7
    body = json.loads(seen[0].content)
    assert body["messages"][0] == {"role": "system", "content": "sys"} and body["tools"][0]["function"]["name"] == "echo"
    assert seen[0].headers["Authorization"] == "Bearer k"
    with pytest.raises(ProviderDown):
        p.chat("m", "sys", [{"role": "user", "content": "hi"}], [], 100)


def test_cloud_provider_without_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    assert not OpenAICompatProvider("openai", "https://x/v1", "NOPE_KEY").available()


# Router ------------------------------------------------------------------------------------------

def test_router_fails_over_and_records_spend(conn, spend):
    cloud = Scripted("claude", [ProviderDown("boom")])
    backup = Scripted("openai", [say("from backup", 1_000_000, 0)])
    r = router_with([cloud, backup], {"default": ["claude:a", "openai:b"]}, spend, conn).chat("default", "s", [], [])
    assert r.text == "from backup"
    assert conn.execute("SELECT model FROM spend").fetchone()[0] == "openai:b"
    assert conn.execute("SELECT kind FROM audit").fetchone()[0] == "model_failover"


def test_private_mode_never_reaches_the_cloud(conn, spend):
    cloud = Scripted("claude", [say("cloud")])
    local = Scripted("mlx", [say("local", 5000, 5000)], location="local")
    router = router_with([cloud, local], {"default": ["claude:a"], "private": ["mlx:q"]}, spend, conn)
    assert router.chat("default", "s", [], [], private=True).text == "local"
    assert cloud.calls == [] and spend.month() == 0


def test_spend_cap_skips_cloud_but_local_still_answers(conn):
    capped = Spend(conn, 1.4, 5, 0)
    cloud = Scripted("claude", [say("cloud")])
    local = Scripted("ollama", [say("local")], location="local")
    router = router_with([cloud, local], {"default": ["claude:a", "ollama:q"]}, capped, conn)
    assert router.chat("default", "s", [], []).text == "local" and cloud.calls == []


def test_no_model_available_names_what_was_tried():
    down = Scripted("ollama", [], location="local", up=False)
    with pytest.raises(NoModelAvailable, match="ollama:q unavailable"):
        router_with([down], {"default": ["ollama:q"]}).chat("default", "s", [], [])


def test_router_rejects_unknown_provider_in_chain():
    with pytest.raises(ValueError):
        router_with([Scripted("claude", [])], {"default": ["gpt:x"]})


# Profiles ----------------------------------------------------------------------------------------

def test_profile_stances_can_tighten_but_never_loosen():
    reg = ToolRegistry([echo_tool("echo"), echo_tool("lock", approval=True), echo_tool("shell")])
    p = Profile("t", allow=("echo", "lock"), ask=("echo",), block=("shell",))
    tools = p.tools(reg)
    assert tools.get("shell") is None
    assert tools.get("echo").needs_approval({}) is True       # ask makes it stricter
    assert tools.get("lock").needs_approval({}) is True       # allow can't remove the tool's own rule


def test_load_profiles_keeps_default_assistant():
    ps = load_profiles({"researcher": {"model": "planning", "tools": {"allow": ["web_*"]}, "private": True}})
    assert set(ps) == {"assistant", "researcher"} and ps["researcher"].private
    assert ps["researcher"].stance("web_search") == "allow" and ps["researcher"].stance("ha_call_service") == "block"


# Loop --------------------------------------------------------------------------------------------

def make_agent(conn, results, tools=(), approvals=None, skills=None):
    p = Scripted("claude", results)
    router = router_with([p], {"default": ["claude:m"]})
    return HarnessAgent(router, ToolRegistry(list(tools)), AuditLog(conn), approvals, skills, sleep=lambda s: None), p


def test_loop_runs_tools_and_returns_final_text(conn):
    agent, p = make_agent(conn, [call("echo", {"x": "hi"}), say("All done")], [echo_tool()])
    r = agent.run([{"role": "user", "content": "go"}], Profile("assistant"))
    assert r.text == "All done" and r.models == ["claude:m", "claude:m"]
    assert p.calls[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": '{"echo": "hi"}', "is_error": False}


def test_loop_blocked_tool_is_unknown_to_the_model(conn):
    agent, p = make_agent(conn, [call("echo", {}), say("ok")], [echo_tool()])
    agent.run([{"role": "user", "content": "go"}], Profile("assistant", block=("echo",)))
    assert p.calls[0]["tools"] == [] and p.calls[1]["messages"][-1]["is_error"]


def test_chat_returns_pending_approval_without_running_the_tool(conn, phone):
    ran = []
    t = Tool("unlock", "u", {"type": "object", "properties": {}}, lambda i: ran.append(1), lambda i: True)
    approvals = Approvals([phone.public_pem])
    agent, _ = make_agent(conn, [call("unlock", {}), say("Waiting for approval")], [t], approvals)
    r = agent.run([{"role": "user", "content": "unlock"}], Profile("assistant"))
    assert ran == [] and len(r.pending_approvals) == 1


def test_task_mode_waits_for_the_phone_then_runs(conn, phone):
    ran = []
    t = Tool("unlock", "u", {"type": "object", "properties": {}}, lambda i: ran.append(1) or "unlocked", lambda i: True)
    approvals = Approvals([phone.public_pem])
    agent, _ = make_agent(conn, [call("unlock", {}), say("Unlocked")], [t], approvals)

    def approve_on_first_poll(_s):
        req = approvals.pending()[0]
        approvals.decide(req.id, "approve", phone.sign(req, "approve"))
    agent.sleep = approve_on_first_poll
    r = agent.run([{"role": "user", "content": "unlock"}], Profile("assistant"), hooks=Hooks(wait_for_approval=True))
    assert ran == [1] and r.text == "Unlocked"


def test_token_budget_stops_the_loop(conn):
    agent, _ = make_agent(conn, [call("echo", {}), say("never")], [echo_tool()])
    r = agent.run([{"role": "user", "content": "go"}], Profile("assistant", token_budget=5))
    assert r.stop == "budget"


# Skills ------------------------------------------------------------------------------------------

def test_skills_read_agent_skills_and_flat_files(tmp_path):
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "SKILL.md").write_text("---\nname: research\ndescription: Use for research.\n---\n\n# R\n1. Search.")
    (tmp_path / "ttc.md").write_text("# TTC\n\nUse for TTC reports.\n\n1. Download.")
    (tmp_path / "README.md").write_text("ignored")
    lib = SkillLibrary(tmp_path)
    assert lib.names() == ["research", "ttc"] and lib.get("ttc").description == "Use for TTC reports."
    tool = lib.tool(["research"])
    assert "Search." in tool.handler({"name": "research"}) and "No skill" in tool.handler({"name": "ttc"})
    assert "research: Use for research." in lib.prompt(["*"])


def test_repo_skills_load():
    lib = SkillLibrary("skills")
    assert {"deep-research", "weekly-reflection", "ttc-reimbursement"} <= set(lib.names())


# Tasks -------------------------------------------------------------------------------------------

def make_runner(conn, tmp_path, results, tools=()):
    agent, p = make_agent(conn, results, tools)
    store = Store(conn, tmp_path)
    return TaskRunner(store, agent, {"assistant": Profile("assistant")}, start=False), store, p


def test_task_runs_saves_artifact_and_notifies(conn, tmp_path):
    runner, store, _ = make_runner(conn, tmp_path, [call("save_artifact", {"name": "report.md", "content": "# Hi"}), say("Saved")])
    done = []
    runner.notify = done.append
    t = runner.submit("write a report")
    runner.run_next()
    t = store.task(t["id"])
    assert t["state"] == "done" and t["result"] == "Saved" and done[0]["id"] == t["id"]
    (art,) = store.artifacts(t["id"])
    assert store.artifact(art["id"])[1] == b"# Hi"
    assert [e["kind"] for e in store.events(t["id"])] == ["queued", "model", "tool", "model", "done"]


def test_cancel_queued_task(conn, tmp_path):
    runner, store, p = make_runner(conn, tmp_path, [say("never")])
    t = runner.submit("x")
    assert runner.cancel(t["id"]) and store.task(t["id"])["state"] == "cancelled"
    runner.run_next()
    assert p.calls == []


def test_steering_reaches_the_model_between_steps(conn, tmp_path):
    runner, store, p = make_runner(conn, tmp_path, [say("ok")])
    t = runner.submit("plan a trip")
    runner.steer(t["id"], "only by train")
    runner.run_next()
    assert p.calls[0]["messages"][-1]["content"] == "[Steering from the owner] only by train"


def test_attachments_become_images_and_text(conn, tmp_path):
    runner, store, p = make_runner(conn, tmp_path, [say("ok")])
    runner.submit("look", attachments=[{"name": "a.png", "media_type": "image/png", "data": "AAAA"},
                                       {"name": "n.txt", "media_type": "text/plain", "data": base64.b64encode(b"note").decode()}])
    runner.run_next()
    first = p.calls[0]["messages"][0]
    assert first["images"][0]["media_type"] == "image/png" and "note" in first["content"]


def test_failed_model_marks_task_failed(conn, tmp_path):
    runner, store, _ = make_runner(conn, tmp_path, [ProviderDown("down")])
    t = runner.submit("x")
    runner.run_next()
    assert store.task(t["id"])["state"] == "failed"


# Heartbeat, channels, MCP ------------------------------------------------------------------------

def test_heartbeat_ok_is_silent_and_heads_up_is_delivered(tmp_path):
    out = []
    p = Scripted("ollama", [say("HEARTBEAT_OK"), say("HEADS UP: a task waits for approval")], location="local")
    hb = Heartbeat(router_with([p], {"fast": ["ollama:q"]}), tmp_path / "none.md", lambda: "state", out.append)
    noon = datetime(2026, 10, 22, 12, 0)
    assert hb.beat(noon).message is None and out == []
    b = hb.beat(noon)
    assert b.message == "a task waits for approval" and b.urgency.name == "HEADS_UP" and out == [b]
    assert hb.beat(datetime(2026, 10, 22, 3, 0)).message is None and len(p.calls) == 2


class FakeChannel:
    name = "telegram"

    def __init__(self, inbox):
        self.inbox, self.sent = list(inbox), []

    def poll(self):
        out, self.inbox = self.inbox, []
        return out

    def send(self, sender, text):
        self.sent.append((sender, text))


def test_gateway_pairs_unknown_senders_before_they_can_chat():
    ch = FakeChannel([("42", "hi")])
    pairing = Pairing()
    gw = Gateway([ch], lambda key, text, channel: f"echo {text}", pairing)
    assert gw.poll_once() == 0 and "Pairing code" in ch.sent[0][1]
    code = next(iter(pairing.codes))
    assert pairing.approve(code) == "telegram:42"
    ch.inbox = [("42", "hi again")]
    assert gw.poll_once() == 1 and ch.sent[-1] == ("42", "echo hi again")


def test_mcp_tools_ask_unless_read_only_or_allowed():
    class Client:
        def list_tools(self):
            return [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in ("list_issues", "create_issue", "merge_pr")]

        def call(self, name, args):
            return f"{name} ok"
    tools = {t.name: t for t in wrap_server("gh", Client(), ask=["merge_*"], allow=["create_issue"])}
    assert not tools["gh__list_issues"].needs_approval({})
    assert not tools["gh__create_issue"].needs_approval({})
    assert tools["gh__merge_pr"].needs_approval({})
    assert tools["gh__list_issues"].handler({}) == "list_issues ok"


# API ---------------------------------------------------------------------------------------------

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    from home_agents.api import build_app
    from home_agents.settings import Settings

    monkeypatch.setenv("HOME_AGENTS_SERVICE_TOKEN", "t0k")
    monkeypatch.setenv("HOME_AGENTS_DATA", str(tmp_path))
    monkeypatch.setenv("HOOK_TEST_SECRET", "s3cret")
    p = Scripted("claude", [say("Hello from the harness"), say("hook handled")])
    cfg = {"models": {"default": ["claude:m"]}, "skills_dir": "skills",
           "hooks": {"test": {"secret_env": "HOOK_TEST_SECRET", "prompt": "Handle this"}}}
    app = build_app(Settings(), client=object(), harness_config=cfg, providers={"claude": p}, start_workers=False)
    return TestClient(app), app, p


H = {"Authorization": "Bearer t0k"}


def test_api_chat_session_and_overview(api_client):
    c, app, _ = api_client
    r = c.post("/v1/h/chat", json={"text": "hi"}, headers=H).json()
    assert r["text"] == "Hello from the harness" and r["session"]["title"] == "hi"
    msgs = c.get(f"/v1/h/sessions/{r['session']['id']}/messages", headers=H).json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    o = c.get("/v1/h/overview", headers=H).json()
    assert o["chains"] == {"default": ["claude:m"]} and "deep-research" in o["skills"]
    assert c.post("/v1/h/chat", json={"text": "hi"}).status_code == 401


def test_api_webhook_needs_signature_and_creates_task(api_client):
    from home_agents.harness.api import sign
    c, app, _ = api_client
    c.post("/v1/h/chat", json={"text": "hi"}, headers=H)
    body = b'{"monitor":"vault","status":"down"}'
    assert c.post("/v1/hooks/test", content=body, headers={"X-Signature": "bad"}).status_code == 401
    assert c.post("/v1/hooks/nope", content=body).status_code == 404
    tid = c.post("/v1/hooks/test", content=body, headers={"X-Signature": sign("s3cret", body)}).json()["task"]
    app.state.harness.tasks.run_next()
    t = c.get(f"/v1/h/tasks/{tid}", headers=H).json()
    assert t["state"] == "done" and t["source"] == "hook:test"


def test_api_console_and_projects(api_client):
    c, _, _ = api_client
    page = c.get("/console")
    assert page.status_code == 200 and "Home agents" in page.text and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    p = c.post("/v1/h/projects", json={"name": "Taxes", "instructions": "CAD only"}, headers=H).json()
    assert c.get("/v1/h/projects", headers=H).json()[0]["id"] == p["id"]


def test_api_artifacts_download_never_render(api_client):
    c, app, _ = api_client
    a = app.state.harness.store.save_artifact('x"<script>.html', b"<script>alert(1)</script>", "text/html")
    r = c.get(f"/v1/h/artifacts/{a['id']}", headers=H)
    assert r.headers["content-type"] == "application/octet-stream" and r.headers["content-security-policy"] == "sandbox"
    assert '"' not in r.headers["content-disposition"].split("filename=")[1].strip('"')
