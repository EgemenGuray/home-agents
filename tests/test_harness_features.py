"""Features from the second comparison (Claude app, Codex, Claude Code, Kiro, CrewAI): hooks, structured
output, coding workspaces, plan and spec modes, crews and flows, reliability, subagents, compaction,
temporary chat, voice through the harness, the API and the CLI."""

import json
import subprocess
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from home_agents.approvals import Approvals
from home_agents.audit import AuditLog
from home_agents.harness import cli
from home_agents.harness.coding import LocalCodeWorkspaces
from home_agents.harness.crews import CrewError, CrewRunner
from home_agents.harness.hooks import EventHooks
from home_agents.harness.loop import HarnessAgent, Hooks
from home_agents.harness.outputs import check, instructions, schema_errors
from home_agents.harness.profiles import Profile
from home_agents.harness.store import Store
from home_agents.harness.tasks import Halted, TaskRunner
from home_agents.tools import ToolRegistry
from test_harness import Scripted, call, echo_tool, make_agent, make_runner, router_with, say


def names(tools):
    return [t["name"] for t in tools]


# Hooks (Claude Code and Kiro) --------------------------------------------------------------------

def test_hooks_block_or_ask_before_a_tool_runs(conn):
    agent, p = make_agent(conn, [call("echo", {"x": "preheat the oven"}), call("echo2", {"x": "y"}, "c2"), say("done")],
                          [echo_tool(), echo_tool("echo2")])
    agent.events = EventHooks([{"on": "tool_before", "match": {"tool": "echo", "input_contains": "oven"}, "do": "block"},
                               {"on": "tool_before", "match": {"tool": "echo2"}, "do": "ask"}])
    agent.run([{"role": "user", "content": "go"}], Profile("assistant"))
    assert "household rule blocks" in p.calls[1]["messages"][-1]["content"]
    assert "needs the owner's approval" in p.calls[2]["messages"][-1]["content"]


def test_hooks_notify_and_start_tasks_but_never_loop():
    sent, started = [], []
    h = EventHooks([{"on": "task_done", "do": "notify", "message": "Done: {title}"},
                    {"on": "task_failed", "do": "task", "prompt": "Investigate {title}: {result}", "title": "Investigate"}],
                   notify=sent.append, start_task=lambda *a: started.append(a))
    h.emit("task_done", {"title": "Report", "source": "app"})
    h.emit("task_failed", {"title": "Backup", "result": "disk full", "source": "app"})
    h.emit("task_failed", {"title": "Investigate", "result": "x", "source": "rule:Investigate"})
    assert sent == ["Done: Report"] and started == [("Investigate Backup: disk full", "assistant", "Investigate")]
    with pytest.raises(ValueError):
        EventHooks([{"on": "task_done", "do": "block"}])
    with pytest.raises(ValueError):
        EventHooks([{"on": "whenever", "do": "notify"}])
    import yaml
    assert EventHooks(yaml.safe_load("- {on: task_done, do: notify}")).rules[0]["on"] == "task_done"  # YAML reads on: as true


# Structured output and guardrails (CrewAI) ---------------------------------------------------------

def test_schema_and_guardrail_checks():
    schema = {"type": "object", "required": ["options"],
              "properties": {"options": {"type": "array", "minItems": 2, "items": {"type": "string"}}}}
    problems, parsed = check('Here:\n```json\n{"options": ["a", "b"]}\n```', schema, {"must_include": ["here"], "max_words": 50})
    assert problems == [] and parsed == {"options": ["a", "b"]}
    problems, _ = check('{"options": [1]}', schema, {"must_not_include": ["options"], "regex": "^Z"})
    assert "$.options needs at least 2 items" in problems and "$.options[0] should be string" in problems
    assert any("must not contain" in x for x in problems) and any("must match" in x for x in problems)
    assert check("no json here", schema)[0] == ["no JSON found in the result"]
    assert schema_errors(True, {"type": "integer"}) == ["$ should be integer"]
    assert schema_errors("x", {"enum": ["a"]}) == ["$ should be one of ['a']"]
    assert "Expected output: a table" in instructions("a table", schema) and "```json" in instructions("", schema)


# Coding workspaces (Codex, Claude Code, Kiro) -------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "src"
    (src / ".kiro" / "steering").mkdir(parents=True)

    def git(*a):
        subprocess.run(["git", *a], cwd=src, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    (src / "AGENTS.md").write_text("Run pytest before you finish.")
    (src / ".kiro" / "steering" / "style.md").write_text("Use type hints.")
    (src / "app.py").write_text("def add(a, b):\n    return a - b\n")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@users.noreply.example", "commit", "-qm", "init")
    return str(src)


def test_coding_workspace_branch_context_edit_checkpoint_rewind(repo, tmp_path):
    ws = LocalCodeWorkspaces(tmp_path / "ws").open(repo, "task_1")
    ctx = ws.prepare()
    assert "Run pytest" in ctx and "Use type hints" in ctx and "can't widen your permissions" in ctx
    assert ws.sh("git branch --show-current").out.strip() == "agent/task_1"
    t = {x.name: x for x in ws.tools()}
    assert "2\t    return a - b" in t["read_file"].handler({"path": "app.py"})
    t["edit_file"].handler({"path": "app.py", "old": "a - b", "new": "a + b"})
    assert "a + b" in t["git_diff"].handler({})
    cp = t["checkpoint"].handler({"message": "fix add"}).split()[-1]
    assert "fix add" in t["list_checkpoints"].handler({})
    t["write_file"].handler({"path": "notes/todo.md", "content": "later"})
    assert "notes/todo.md" in t["list_files"].handler({})
    assert "app.py:2" in t["grep"].handler({"pattern": "a \\+ b"})
    t["rewind"].handler({"checkpoint": cp})
    assert "notes/todo.md" not in t["list_files"].handler({})
    assert t["run_command"].handler({"command": "python3 -c 'import app; assert app.add(2, 2) == 4'"}).startswith("exit 0")
    assert t["open_pull_request"].needs_approval({"title": "x", "body": "y"})


def test_coding_workspace_stays_inside_the_repo(repo, tmp_path):
    ws = LocalCodeWorkspaces(tmp_path / "ws").open(repo, "t2")
    for bad in ("../outside.txt", "/../../x", ".git/config"):
        with pytest.raises(ValueError):
            ws.path(bad)
    with pytest.raises(ValueError, match="exactly once"):
        ws.edit_file({"path": "app.py", "old": "zzz", "new": "y"})
    assert {t.name for t in ws.tools(read_only=True)} == {"read_file", "list_files", "grep", "git_diff", "list_checkpoints"}


def test_coding_task_gets_its_branch_context_and_tools(conn, tmp_path, repo):
    runner, store, p = make_runner(conn, tmp_path, [call("edit_file", {"path": "app.py", "old": "a - b", "new": "a + b"}), say("Fixed")],
                                   code_workspaces=LocalCodeWorkspaces(tmp_path / "ws"))
    t = runner.submit("fix add", options={"repo": repo})
    runner.run_next()
    assert store.task(t["id"])["state"] == "done" and "Run pytest" in p.calls[0]["system"]
    assert "edit_file" in names(p.calls[0]["tools"])
    ws = [e for e in store.events(t["id"]) if e["kind"] == "workspace"]
    assert ws[0]["data"]["branch"] == f"agent/{t['id']}"


def test_coding_task_without_workspaces_fails_clearly(conn, tmp_path):
    runner, store, _ = make_runner(conn, tmp_path, [])
    t = runner.submit("fix", options={"repo": "https://example.com/r.git"})
    runner.run_next()
    assert store.task(t["id"])["state"] == "failed" and "not configured" in store.task(t["id"])["result"]


# Plan and spec modes, questions --------------------------------------------------------------------

def answering(runner, store, answers, seen):
    answers = list(answers)

    def sleep(_):
        q = store.questions("open")[0]
        seen.append(q["question"])
        runner.answer(q["id"], answers.pop(0))
    return sleep


def test_plan_mode_is_read_only_until_the_owner_says_ok(conn, tmp_path):
    runner, store, p = make_runner(conn, tmp_path, [say("1. echo"), call("echo", {"x": "go"}), say("Done")], [echo_tool()])
    seen = []
    runner.sleep = answering(runner, store, ["ok"], seen)
    t = runner.submit("tidy up", mode="plan")
    runner.run_next()
    assert "echo" not in names(p.calls[0]["tools"]) and "echo" in names(p.calls[1]["tools"])
    assert seen[0].startswith("Approve this plan") and store.task(t["id"])["state"] == "done"
    assert [a["name"] for a in store.artifacts(t["id"])] == ["plan.md"]


def test_spec_mode_writes_requirements_design_tasks_with_owner_review(conn, tmp_path):
    replies = [say("req v1"), say("req v2 with offline case"), say("design"), say("tasks"), say("built")]
    runner, store, p = make_runner(conn, tmp_path, replies)
    seen = []
    runner.sleep = answering(runner, store, ["add an offline case", "ok", "yes"], seen)
    t = runner.submit("shopping list sync", mode="spec")
    runner.run_next()
    arts = {a["name"]: store.artifact(a["id"])[1] for a in store.artifacts(t["id"])}
    assert arts == {"requirements.md": b"req v2 with offline case", "design.md": b"design", "tasks.md": b"tasks"}
    assert "EARS" in p.calls[0]["messages"][-1]["content"] and "[Owner] add an offline case" in p.calls[1]["messages"][-1]["content"]
    assert store.task(t["id"])["result"] == "built" and len(seen) == 3


def test_ask_owner_waits_for_the_answer_and_notifies(conn, tmp_path):
    runner, store, p = make_runner(conn, tmp_path, [call("ask_owner", {"question": "Which colour?", "choices": ["blue", "red"]}),
                                                    say("Going with blue")])
    notes, seen = [], []
    runner.notify = notes.append
    runner.sleep = answering(runner, store, ["blue"], seen)
    t = runner.submit("paint the fence")
    runner.run_next()
    assert seen == ["Which colour?"] and p.calls[1]["messages"][-1]["content"] == "blue"
    assert notes[0]["state"] == "waiting_input" and store.task(t["id"])["state"] == "done"


def test_todos_and_progress_notes(conn, tmp_path):
    runner, store, _ = make_runner(conn, tmp_path, [call("update_todos", {"items": [{"text": "a", "status": "doing"}]}),
                                                    call("notify_owner", {"message": "halfway"}, "c2"), say("ok")])
    notes = []
    runner.notify = notes.append
    t = runner.submit("work")
    runner.run_next()
    kinds = {e["kind"]: e["data"] for e in store.events(t["id"])}
    assert kinds["todos"]["items"][0]["status"] == "doing" and notes[0]["result"] == "halfway"


# Reliability ---------------------------------------------------------------------------------------

def test_recover_requeues_tasks_that_were_running(conn, tmp_path):
    runner, store, _ = make_runner(conn, tmp_path, [say("finished")])
    t = store.create_task("x", "x", "assistant")
    store.set_task_state(t["id"], "running")
    assert runner.recover() == [t["id"]]
    runner.run_next()
    assert store.task(t["id"])["state"] == "done"
    assert "requeued" in [e["kind"] for e in store.events(t["id"])]


def test_pause_and_resume(conn, tmp_path):
    runner, store, _ = make_runner(conn, tmp_path, [say("done")])
    t = runner.submit("long job")
    assert runner.pause(t["id"]) and not runner.resume("task_nope")
    runner.agent.sleep = lambda _s: runner.resume(t["id"])
    runner.run_next()
    kinds = [e["kind"] for e in store.events(t["id"])]
    assert "paused" in kinds and "resumed" in kinds and store.task(t["id"])["state"] == "done"


def test_emergency_stop_cancels_everything_and_refuses_new_work(conn, tmp_path):
    runner, store, _ = make_runner(conn, tmp_path, [say("x")])
    a, b = runner.submit("a"), runner.submit("b")
    assert runner.halt() == 2 and {store.task(a["id"])["state"], store.task(b["id"])["state"]} == {"cancelled"}
    with pytest.raises(Halted):
        runner.submit("c")
    runner.resume_all()
    assert runner.submit("d")["state"] == "queued"


def test_task_model_override(conn, tmp_path):
    p = Scripted("claude", [say("deep answer")])
    agent = HarnessAgent(router_with([p], {"default": ["claude:small"], "planning": ["claude:big"]}), ToolRegistry([]),
                         AuditLog(conn), sleep=lambda s: None)
    runner = TaskRunner(Store(conn, tmp_path), agent, {"assistant": Profile("assistant")}, start=False)
    runner.submit("think hard", options={"model": "planning"})
    runner.run_next()
    assert p.calls[0]["model"] == "big"


# Crews and flows (CrewAI) ----------------------------------------------------------------------------

CREW = {"agents": {"researcher": {"role": "Travel researcher", "goal": "find options"},
                   "writer": {"role": "Planner", "goal": "write the plan"}},
        "tasks": [{"name": "options", "agent": "researcher", "description": "Options for {destination}",
                   "guardrail": {"must_include": ["CAD"]}},
                  {"name": "plan", "agent": "writer", "description": "Make the plan", "context": ["options"]}]}


def crew_runner(conn, results, crews=None, flows=None):
    agent, p = make_agent(conn, results)
    return CrewRunner(agent, {"assistant": Profile("assistant")}, crews or {"trip": CREW}, flows or {}), p


def test_sequential_crew_retries_a_failed_guardrail_and_passes_context(conn):
    crews, p = crew_runner(conn, [say("Three hotels"), say("Three hotels from 120 CAD"), say("Day 1: arrive")])
    out = crews.run_crew("trip", {"destination": "Lisbon"})
    assert out["final"] == "Day 1: arrive" and "CAD" in out["results"]["options"]
    assert "Options for Lisbon" in p.calls[0]["messages"][0]["content"] and "You are the Travel researcher" in p.calls[0]["system"]
    assert "must mention 'CAD'" in p.calls[1]["messages"][-1]["content"]
    assert "Result of options" in p.calls[2]["messages"][0]["content"]


def test_crew_gives_up_after_retries(conn):
    crews, _ = crew_runner(conn, [say("no prices")] * 3)
    with pytest.raises(CrewError, match="failed its checks"):
        crews.run_crew("trip", {"destination": "Lisbon"})
    with pytest.raises(CrewError):
        crews.run_crew("nope", {})


def test_hierarchical_crew_manager_delegates(conn):
    crew = {**CREW, "process": "hierarchical", "tasks": [{"description": "Plan {destination}", "expected_output": "a plan"}]}
    crews, p = crew_runner(conn, [call("delegate_to", {"agent": "researcher", "task": "find hotels"}), say("Hotel A"),
                                  say("Final plan with Hotel A")], {"trip": crew})
    out = crews.run_crew("trip", {"destination": "Lisbon"})
    assert out["final"] == "Final plan with Hotel A" and out["results"] == {"researcher-1": "Hotel A"}
    assert "Travel researcher" in p.calls[0]["messages"][0]["content"] and "delegate_to" in names(p.calls[0]["tools"])


def test_human_input_lets_the_owner_revise_a_step(conn):
    crew = {"agents": CREW["agents"], "tasks": [{"name": "plan", "agent": "writer", "description": "Plan", "human_input": True}]}
    crews, _ = crew_runner(conn, [say("a very long plan"), say("short plan")], {"trip": crew})
    asked = []
    out = crews.run_crew("trip", {}, ask=lambda q: asked.append(q) or "make it shorter")
    assert out["final"] == "short plan" and "Review the result" in asked[0]


def test_flow_routes_and_conditions(conn):
    flows = {"triage": {"steps": [
        {"name": "kind", "prompt": "Is '{request}' a bug or an idea?", "route": {"bug": "fix", "idea": "log"}},
        {"name": "fix", "prompt": "Fix {request}", "next": "end"},
        {"name": "log", "prompt": "Log {request}"}]},
        "check": {"steps": [{"name": "a", "prompt": "anything?"},
                            {"name": "b", "prompt": "follow up", "when": {"step": "a", "contains": "yes"}}]}}
    crews, p = crew_runner(conn, [say("bug"), say("fixed it"), say("no")], flows=flows)
    out = crews.run_flow("triage", {"request": "crash on start"})
    assert out["state"]["kind"] == "bug" and out["final"] == "fixed it" and "log" not in out["state"]
    assert crews.run_flow("check", {})["final"] == "no" and len(p.calls) == 3


def test_flow_loop_guard(conn):
    flows = {"spin": {"steps": [{"name": "a", "prompt": "again?", "route": {"again": "a", "done": "end"}}]}}
    crews, _ = crew_runner(conn, [say("again")] * 6, flows=flows)
    with pytest.raises(CrewError, match="looping"):
        crews.run_flow("spin", {})


def test_crew_mode_task_saves_the_result(conn, tmp_path):
    agent, p = make_agent(conn, [say("Three hotels from 120 CAD"), say("Day 1: arrive")])
    profiles = {"assistant": Profile("assistant")}
    runner = TaskRunner(Store(conn, tmp_path), agent, profiles, start=False, crews=CrewRunner(agent, profiles, {"trip": CREW}))
    t = runner.submit("Lisbon trip", mode="crew", options={"crew": "trip", "inputs": {"destination": "Lisbon"}})
    runner.run_next()
    (art,) = runner.store.artifacts(t["id"])
    assert runner.store.task(t["id"])["result"] == "Day 1: arrive" and art["name"] == "crew-result.md"


# Agent loop: subagents, advisor, compaction, lessons, approvals after the turn -----------------------

def test_delegate_task_runs_a_subagent_with_depth_limit(conn):
    agent, p = make_agent(conn, [call("delegate_task", {"profile": "researcher", "prompt": "find x"}), say("found x"), say("Done: x")])
    agent.profiles = {"assistant": Profile("assistant"), "researcher": Profile("researcher", persona="You research carefully.")}
    r = agent.run([{"role": "user", "content": "go"}], Profile("assistant"))
    assert r.text == "Done: x" and "You research carefully." in p.calls[1]["system"]
    assert "[researcher finished: end] found x" in p.calls[2]["messages"][-1]["content"]
    deep, p2 = make_agent(conn, [say("ok")])
    deep.profiles = agent.profiles
    deep.run([{"role": "user", "content": "go"}], Profile("assistant"), depth=2)
    assert "delegate_task" not in names(p2.calls[0]["tools"])


def test_second_opinion_asks_the_advisor_chain(conn):
    main, other = Scripted("claude", [call("second_opinion", {"question": "Is this plan safe?"}), say("ok")]), Scripted("gpt", [say("Risky")])
    agent = HarnessAgent(router_with([main, other], {"default": ["claude:m"], "advisor": ["gpt:o"]}), ToolRegistry([]),
                         AuditLog(conn), sleep=lambda s: None)
    agent.run([{"role": "user", "content": "go"}], Profile("assistant"))
    assert main.calls[1]["messages"][-1]["content"].endswith(": Risky") and other.calls[0]["model"] == "o"


def test_long_runs_are_compacted(conn):
    agent, p = make_agent(conn, [say("summary of the old steps"), say("final")])
    agent.compact_at = 500
    history = []
    for i in range(6):
        history += [{"role": "user", "content": f"step {i} " + "x" * 100}, {"role": "assistant", "content": "ok " + "y" * 100}]
    events = []
    r = agent.run(history + [{"role": "user", "content": "finish"}], Profile("assistant"), hooks=Hooks(lambda k, d: events.append(k)))
    sent = p.calls[1]["messages"]
    assert r.text == "final" and "[Summary of earlier work" in sent[1]["content"] and len(sent) < len(history)
    assert sent[0]["content"].startswith("step 0") and "compacted" in events


def test_feedback_becomes_lessons_in_the_system_prompt(conn, tmp_path):
    store = Store(conn, tmp_path)
    store.add_feedback(-1, "Don't use bullet lists", profile="assistant")
    store.add_feedback(1, "Short answers are great", profile="assistant")
    store.add_feedback(1, "", profile="assistant")
    agent, p = make_agent(conn, [say("ok")])
    agent.lessons = store.lessons
    agent.run([{"role": "user", "content": "hi"}], Profile("assistant"))
    assert "Avoid: Don't use bullet lists" in p.calls[0]["system"] and "Keep doing: Short answers are great" in p.calls[0]["system"]


def test_chat_approval_runs_the_tool_when_approved_and_reports_back(conn, phone):
    ran, said = [], []
    tool = replace(echo_tool(approval=True), handler=lambda i: ran.append(i) or "unlocked")
    approvals = Approvals([phone.public_pem], audit=AuditLog(conn))
    agent, _ = make_agent(conn, [call("echo", {"x": "front door"}), say("Waiting for your approval")], [tool], approvals=approvals)
    agent.followup = lambda text, channel: said.append((text, channel))
    r = agent.run([{"role": "user", "content": "unlock"}], Profile("assistant"), channel="voice in the hall")
    req = approvals.get(r.pending_approvals[0])
    assert ran == []
    approvals.decide(req.id, "approve", phone.sign(req, "approve"))
    assert ran == [{"x": "front door"}] and said[0][0].startswith("Done: echo") and said[0][1] == "voice in the hall"


# Harness, voice, API and CLI -------------------------------------------------------------------------

H = {"Authorization": "Bearer t0k"}


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    from home_agents.api import build_app
    from home_agents.settings import Settings

    monkeypatch.setenv("HOME_AGENTS_SERVICE_TOKEN", "t0k")
    monkeypatch.setenv("HOME_AGENTS_DATA", str(tmp_path))
    p = Scripted("claude", [])
    cfg = {"models": {"default": ["claude:m"], "planning": ["claude:big"]}, "skills_dir": "skills",
           "routines": {"defaults": [{"name": "Good night", "triggers": [{"type": "phrase", "phrases": ["good night"]}],
                                      "steps": [{"say": "Sleep well", "speaker": False}]}]}}
    app = build_app(Settings(), client=object(), harness_config=cfg, providers={"claude": p}, start_workers=False)
    return TestClient(app), app.state.harness, p


def test_temporary_chat_stores_and_remembers_nothing(app_client):
    c, h, p = app_client
    p.results += [say("hi")]
    r = c.post("/v1/h/chat", json={"text": "secret plans", "temporary": True}, headers=H).json()
    assert r["session"] is None and r["text"] == "hi" and h.store.sessions() == []
    assert not {"recall_memory", "save_memory", "remember_fact"} & set(names(p.calls[0]["tools"]))
    assert h.recall.search("secret plans") == []


def test_chat_remembers_and_recalls_across_sessions(app_client):
    c, h, p = app_client
    p.results += [say("Noted"), say("Tuesday")]
    c.post("/v1/h/chat", json={"text": "The dentist is on Tuesday"}, headers=H)
    c.post("/v1/h/chat", json={"text": "when is the dentist?", "model": "planning"}, headers=H)
    assert "Things you remember" in p.calls[1]["system"] and "dentist is on Tuesday" in p.calls[1]["system"]
    assert p.calls[1]["model"] == "big" and "recall_memory" in names(p.calls[1]["tools"])


def test_session_search_and_fork(app_client):
    c, h, p = app_client
    p.results += [say("Lisbon is lovely"), say("Porto too")]
    s = c.post("/v1/h/chat", json={"text": "Tell me about Lisbon"}, headers=H).json()["session"]
    c.post("/v1/h/chat", json={"text": "and Porto?", "session_id": s["id"]}, headers=H)
    assert c.get("/v1/h/search", params={"q": "Lisbon"}, headers=H).json()[0]["session_id"] == s["id"]
    first_answer = h.store.message_ids(s["id"])[1]
    fork = c.post(f"/v1/h/sessions/{s['id']}/fork", json={"upto_message_id": first_answer}, headers=H).json()
    assert [m["content"] for m in h.store.messages(fork["id"])] == ["Tell me about Lisbon", "Lisbon is lovely"]
    assert c.post("/v1/h/sessions/ses_nope/fork", json={}, headers=H).status_code == 404


def test_voice_goes_through_the_harness_with_the_voice_profile(app_client):
    c, h, p = app_client
    p.results += [say("It's 12 degrees."), say("Bring a jacket.")]
    body = {"text": "what's the weather", "conversation_id": "conv-1", "channel": "voice", "room": "kitchen"}
    r = c.post("/v1/converse", json=body, headers=H).json()
    c.post("/v1/converse", json={**body, "text": "should I bring a jacket"}, headers=H)
    assert r == {"text": "It's 12 degrees.", "conversation_id": "conv-1", "pending_approvals": []}
    assert "spoken aloud" in p.calls[0]["system"] and "voice in the kitchen" in p.calls[0]["system"]
    assert [m["content"] for m in p.calls[1]["messages"]][:2] == ["what's the weather", "It's 12 degrees."]


def test_routine_phrase_answers_without_a_model(app_client):
    c, h, p = app_client
    r = c.post("/v1/converse", json={"text": "Good night!", "conversation_id": "c9"}, headers=H).json()
    assert r["text"] == "Sleep well" and p.calls == []
    assert h.routines.runs()[0]["trigger"]["phrase"] == "good night"


def test_api_questions_pause_halt_and_feedback(app_client):
    c, h, p = app_client
    t = c.post("/v1/h/tasks", json={"prompt": "x", "mode": "plan"}, headers=H).json()
    assert t["mode"] == "plan"
    q = h.store.ask(t["id"], "Approve?", ["ok", "no"])
    assert c.get("/v1/h/questions", headers=H).json()[0]["choices"] == ["ok", "no"]
    assert c.post(f"/v1/h/questions/{q['id']}", json={"text": "ok"}, headers=H).json() == {"ok": True}
    assert c.post(f"/v1/h/questions/{q['id']}", json={"text": "ok"}, headers=H).status_code == 404
    assert c.post(f"/v1/h/tasks/{t['id']}/pause", headers=H).json() == {"ok": True}
    assert c.post(f"/v1/h/tasks/{t['id']}/resume", headers=H).json() == {"ok": True}
    assert c.post("/v1/h/tasks", json={"prompt": "x", "mode": "yolo"}, headers=H).status_code == 400
    assert c.post("/v1/h/halt", headers=H).json() == {"cancelled": 1, "halted": True}
    assert c.post("/v1/h/tasks", json={"prompt": "y"}, headers=H).status_code == 409
    assert c.get("/v1/h/metrics", headers=H).json()["halted"] is True
    c.post("/v1/h/resume-all", headers=H)
    assert c.post("/v1/h/tasks", json={"prompt": "y"}, headers=H).status_code == 200
    c.post("/v1/h/feedback", json={"rating": -1, "note": "Too long", "task_id": t["id"]}, headers=H)
    assert h.store.lessons("assistant") == ["Avoid: Too long"]
    m = c.get("/v1/h/metrics", headers=H).json()
    assert m["tasks"]["cancelled"] == 1 and m["tasks"]["queued"] == 1 and m["halted"] is False


def test_api_memory_endpoints(app_client):
    c, h, _ = app_client
    r = c.post("/v1/h/memory/knowledge", json={"title": "Boiler manual", "text": "Hold reset for 5 seconds.\n\nCall the installer."},
               headers=H).json()
    assert r["chunks"] == 1
    hits = c.get("/v1/h/memory", params={"q": "boiler reset"}, headers=H).json()
    assert hits[0]["kind"] == "knowledge" and hits[0]["private"] is True and "embedding" not in hits[0]
    assert c.get("/v1/h/memory/stats", headers=H).json() == {"knowledge": 1}
    assert c.delete(f"/v1/h/memory/{hits[0]['id']}", headers=H).json() == {"archived": hits[0]["id"]}
    assert c.delete("/v1/h/memory/999999", headers=H).status_code == 404
    assert c.get("/v1/h/memory/events", params={"memory_id": hits[0]["id"]}, headers=H).json()[0]["action"] == "archived"
    report = c.post("/v1/h/memory/sleep", headers=H).json()
    assert "merged" in report and report["archived"] == 0


def test_api_streams_task_events_until_the_end(app_client):
    c, h, p = app_client
    p.results += [say("all done")]
    t = c.post("/v1/h/tasks", json={"prompt": "x"}, headers=H).json()
    h.tasks.run_next()
    body = c.get(f"/v1/h/tasks/{t['id']}/stream", headers=H).text
    assert "event: queued" in body and "event: done" in body and body.rstrip().endswith('data: {"state": "done"}')
    assert c.get("/v1/h/tasks/task_nope/stream", headers=H).status_code == 404


def test_cli_drives_the_service(app_client, capsys):
    c, h, p = app_client
    c.headers.update(H)
    p.results += [say("Hello"), say("Report ready")]
    cli.main(["chat", "hi there"], c)
    assert "Hello" in capsys.readouterr().out
    cli.main(["task", "write the report", "--mode", "plan", "--model", "planning"], c)
    tid = capsys.readouterr().out.strip()
    assert h.store.task(tid)["options"] == {"model": "planning"}
    h.tasks.cancel(tid)
    cli.main(["tasks"], c)
    assert "cancelled" in capsys.readouterr().out
    t = h.tasks.submit("another")
    while h.tasks.run_next():
        pass
    cli.main(["watch", t["id"]], c)
    out = capsys.readouterr().out
    assert "[queued]" in out and out.strip().endswith("done")
    cli.main(["recall", "report"], c)
    assert "episode" in capsys.readouterr().out
    cli.main(["halt"], c)
    assert json.loads(capsys.readouterr().out)["halted"] is True
    cli.main(["resume-all"], c)
    with pytest.raises(SystemExit):
        cli.main(["show", "task_nope"], c)


def test_voice_through_the_harness_never_goes_silent_when_models_are_down(app_client):
    from home_agents.harness.providers import ProviderDown
    c, h, p = app_client
    p.results += [ProviderDown("outage")]
    r = c.post("/v1/converse", json={"text": "what's the news", "conversation_id": "c2"}, headers=H).json()
    assert r["degraded"] is True and "Lights, scenes" in r["text"]
