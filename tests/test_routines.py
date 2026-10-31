"""Routines: Alexa Routines and iPhone Shortcuts automations, every trigger, condition and step."""

from datetime import date, datetime, timedelta, timezone

import pytest

from home_agents.approvals import Approvals
from home_agents.audit import AuditLog
from home_agents.harness.loop import HarnessAgent
from home_agents.harness.profiles import Profile
from home_agents.harness.routines import RoutineEngine, RoutineError, describe, sun_times, tool_runner, validate
from home_agents.tools import Tool, ToolRegistry
from test_harness import Scripted, router_with

TZ = timezone(timedelta(hours=-4))
MON_7AM = datetime(2026, 10, 26, 7, 0, tzinfo=TZ)   # a Monday
SAT_7AM = datetime(2026, 10, 31, 7, 0, tzinfo=TZ)
TORONTO = {"latitude": 43.65, "longitude": -79.38}  # a public city centre, not the house


class Env:
    def __init__(self, conn, **kw):
        self.tools: list[tuple[str, dict]] = []
        self.notes: list[str] = []
        self.tasks: list[dict] = []
        self.states: dict[str, str] = {}
        self.slept: list[float] = []
        self.fail_tool = False
        self.audit = AuditLog(conn)
        self.engine = RoutineEngine(conn, run_tool=self.run_tool, ask_agent=lambda p, prof: f"agent says: {p}",
                                    start_task=self.start_task, notify=self.notes.append, audit=self.audit,
                                    states=lambda: self.states, location=TORONTO, sleep=self.slept.append, **kw)

    def run_tool(self, name, inp, job_id):
        self.tools.append((name, inp))
        return ("broken", True) if self.fail_tool else ("ok", False)

    def start_task(self, prompt, **kw):
        self.tasks.append({"prompt": prompt, **kw})
        return {"id": f"task_{len(self.tasks)}"}


@pytest.fixture
def env(conn):
    return Env(conn)


def routine(name="R", triggers=None, steps=None, **kw):
    return {"name": name, "triggers": triggers or [{"type": "manual"}], "steps": steps or [{"notify": "ran {routine}"}], **kw}


# Definitions ------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad, why", [
    ({"name": "", "triggers": [{"type": "manual"}], "steps": [{"stop": True}]}, "needs a name"),
    (routine(triggers=[{"type": "telepathy"}]), "unknown trigger"),
    (routine(triggers=[{"type": "time", "at": "25:00"}]), "bad time"),
    (routine(triggers=[{"type": "time", "at": "07:00", "days": ["funday"]}]), "bad days"),
    (routine(triggers=[{"type": "sun", "event": "noon"}]), "sunrise or sunset"),
    (routine(triggers=[{"type": "presence", "entity": "person.a", "event": "teleport"}]), "arrive or leave"),
    (routine(triggers=[{"type": "webhook", "name": "Bad Name!"}]), "webhook"),
    (routine(triggers=[{"type": "interval", "minutes": 0}]), "minutes >= 1"),
    (routine(steps=[{"dance": True}]), "exactly one of"),
    (routine(steps=[{"say": "a", "notify": "b"}]), "exactly one of"),
    (routine(steps=[{"wait": 7200}]), "up to 3600"),
    (routine(steps=[{"device": {"domain": "light"}}]), "domain and service"),
    (routine(conditions=[{"type": "sun", "is": "sideways"}]), "up or down"),
    (routine(steps=[{"if": [{"type": "mood"}], "then": [{"stop": True}]}]), "unknown condition"),
])
def test_validation_explains_what_is_wrong(bad, why):
    with pytest.raises(RoutineError, match=why):
        validate(bad)


def test_owner_routines_are_on_agent_drafts_wait_for_the_phone(conn, phone):
    approvals = Approvals([phone.public_pem])
    e = Env(conn, approvals=approvals).engine
    mine = e.add(routine("Mine"))
    assert mine["enabled"] and mine["created_by"] == "owner"
    draft = e.add(routine("Draft"), created_by="agent")
    assert not draft["enabled"] and draft["approval_id"]
    assert not e.set_enabled(draft["id"], True, by="agent")
    req = approvals.get(draft["approval_id"])
    assert "Turn on routine 'Draft'" in req.summary
    approvals.decide(req.id, "approve", phone.sign(req, "approve"))
    assert e.get(draft["id"])["enabled"]
    with pytest.raises(RoutineError, match="already exists"):
        e.add(routine("draft"))
    with pytest.raises(RoutineError, match="only the owner"):
        e.update(draft["id"], routine("Draft"), by="agent")


def test_update_delete_and_describe(env):
    r = env.engine.add(routine("Morning", triggers=[{"type": "time", "at": "06:45", "days": "weekdays"},
                                                    {"type": "phrase", "phrases": ["good morning"]}]))
    assert describe(r) == 'at 06:45 on weekdays or "good morning" -> 1 step(s)'
    r = env.engine.update(r["id"], {**r, "steps": [{"stop": True}, {"notify": "x"}]})
    assert len(r["steps"]) == 2 and env.engine.delete(r["id"]) and env.engine.list() == []


# Triggers ---------------------------------------------------------------------------------------

def test_phrase_trigger_matches_near_exact_only(env):
    env.engine.add(routine("Good night", [{"type": "phrase", "phrases": ["good night", "bedtime"]}]))
    assert env.engine.match_phrase("Good night!")[1] == "good night"
    assert env.engine.match_phrase("goodnight")[0]["name"] == "Good night"
    assert env.engine.match_phrase("is it a good night for stargazing") is None
    assert env.engine.match_phrase("") is None


def test_time_trigger_fires_once_on_matching_days(env):
    env.engine.add(routine("Weekday lights", [{"type": "time", "at": "07:00", "days": "weekdays"}]))
    assert len(env.engine.tick(MON_7AM)) == 1
    assert env.engine.tick(MON_7AM + timedelta(seconds=30)) == []          # once a day
    assert env.engine.tick(SAT_7AM) == []                                   # not on weekends
    assert len(env.engine.tick(MON_7AM + timedelta(days=1, minutes=1))) == 1  # a slow tick still catches it
    assert env.engine.tick(MON_7AM + timedelta(days=2, minutes=5)) == []      # but not much later


def test_time_trigger_on_a_date(env):
    env.engine.add(routine("Party", [{"type": "time", "at": "18:00", "date": "2026-12-24"}]))
    assert env.engine.tick(datetime(2026, 12, 24, 18, 0, tzinfo=TZ))[0].state == "done"
    assert env.engine.tick(datetime(2026, 12, 25, 18, 0, tzinfo=TZ)) == []


def test_interval_trigger(env):
    env.engine.add(routine("Every 15", [{"type": "interval", "minutes": 15}]))
    assert env.engine.tick(MON_7AM) == []
    assert env.engine.tick(MON_7AM + timedelta(minutes=10)) == []
    assert len(env.engine.tick(MON_7AM + timedelta(minutes=15))) == 1


def test_sun_times_are_right_for_a_known_city():
    rise, set_ = sun_times(date(2026, 6, 21), 43.65, -79.38)
    assert (rise.hour, set_.hour) == (9, 1)  # 05:36 and 21:03 in Toronto (EDT), in UTC
    assert abs((rise.minute + 60 * rise.hour) - (9 * 60 + 36)) <= 3
    assert sun_times(date(2026, 12, 21), 78.2, 15.6) == (None, None)  # polar night


def test_sun_trigger_with_offset(env):
    env.engine.add(routine("Porch light", [{"type": "sun", "event": "sunset", "offset_minutes": -15}]))
    _, set_ = sun_times(MON_7AM.date(), TORONTO["latitude"], TORONTO["longitude"])
    at = (set_ - timedelta(minutes=15)).astimezone(TZ)
    assert env.engine.tick(at - timedelta(minutes=1)) == []
    assert len(env.engine.tick(at + timedelta(seconds=20))) == 1
    assert env.engine.tick(at + timedelta(seconds=50)) == []


def test_state_trigger_to_from_and_thresholds(env):
    e = env.engine
    e.add(routine("Door", [{"type": "state", "entity": "binary_sensor.front_door", "to": "on", "from": "off"}]))
    e.add(routine("Hot", [{"type": "state", "entity": "sensor.office_temp", "above": 26}]))
    assert e.on_state("binary_sensor.front_door", "on", MON_7AM, old="unavailable") == []
    assert len(e.on_state("binary_sensor.front_door", "on", MON_7AM, old="off")) == 1
    assert e.on_state("sensor.office_temp", "25.5", MON_7AM) == []
    assert len(e.on_state("sensor.office_temp", "26.4", MON_7AM)) == 1
    assert e.on_state("sensor.office_temp", "27", MON_7AM) == []          # still above: no repeat
    e.on_state("sensor.office_temp", "24", MON_7AM)
    assert len(e.on_state("sensor.office_temp", "26.1", MON_7AM)) == 1    # crossed again


def test_state_trigger_must_hold_for_minutes(env):
    env.engine.add(routine("Garage left open", [{"type": "state", "entity": "cover.garage", "to": "open", "for_minutes": 10}]))
    assert env.engine.on_state("cover.garage", "open", MON_7AM, old="closed") == []
    assert env.engine.tick(MON_7AM + timedelta(minutes=5)) == []
    assert len(env.engine.tick(MON_7AM + timedelta(minutes=10))) == 1
    env.engine.on_state("cover.garage", "closed", MON_7AM + timedelta(minutes=11))
    env.engine.on_state("cover.garage", "open", MON_7AM + timedelta(minutes=12))
    env.engine.on_state("cover.garage", "closed", MON_7AM + timedelta(minutes=13))  # closed in time
    assert env.engine.tick(MON_7AM + timedelta(minutes=30)) == []


def test_presence_arrive_and_leave(env):
    env.engine.add(routine("Welcome", [{"type": "presence", "person": "person.owner", "event": "arrive"}]))
    env.engine.add(routine("Bye", [{"type": "presence", "person": "person.owner", "event": "leave", "zone": "home"}]))
    env.engine.add(routine("At work", [{"type": "presence", "person": "person.owner", "event": "arrive", "zone": "work"}]))
    runs = env.engine.on_state("person.owner", "home", MON_7AM, old="not_home")
    assert [env.engine.get(r.routine_id)["name"] for r in runs] == ["Welcome"]
    runs = env.engine.on_state("person.owner", "work", MON_7AM + timedelta(hours=1))
    assert sorted(env.engine.get(r.routine_id)["name"] for r in runs) == ["At work", "Bye"]


def test_events_tags_webhooks_and_harness_events(env):
    e = env.engine
    e.add(routine("Alarm off", [{"type": "event", "event_type": "mobile_app_notification_action", "data": {"action": "ALARM_DISMISSED"}}]))
    e.add(routine("Bedside tag", [{"type": "tag", "tag_id": "bedside-1"}]))
    e.add(routine("Doorbell", [{"type": "webhook", "name": "doorbell"}]))
    e.add(routine("On failure", [{"type": "harness", "event": "task_failed"}], [{"notify": "Task failed: {title}"}]))
    assert e.on_event("mobile_app_notification_action", {"action": "SNOOZE"}, MON_7AM) == []
    assert len(e.on_event("mobile_app_notification_action", {"action": "ALARM_DISMISSED"}, MON_7AM)) == 1
    assert len(e.on_event("tag_scanned", {"tag_id": "bedside-1"}, MON_7AM)) == 1
    assert len(e.on_event("webhook:doorbell", {}, MON_7AM)) == 1
    run = e.on_event("task_failed", {"title": "Backup"}, MON_7AM)[0]
    assert run.state == "done" and env.notes[-1] == "Task failed: Backup"


def test_disabled_routines_never_fire(env):
    r = env.engine.add(routine("Off", [{"type": "phrase", "phrases": ["lights out"]}], enabled=False))
    assert not r["enabled"] and env.engine.match_phrase("lights out") is None
    env.engine.set_enabled(r["id"], True)
    assert env.engine.match_phrase("lights out")


def test_cooldown(env):
    env.engine.add(routine("Motion", [{"type": "state", "entity": "binary_sensor.hall_motion", "to": "on"}], cooldown_minutes=10))
    assert len(env.engine.on_state("binary_sensor.hall_motion", "on", MON_7AM, old="off")) == 1
    env.engine.on_state("binary_sensor.hall_motion", "off", MON_7AM + timedelta(minutes=1))
    assert env.engine.on_state("binary_sensor.hall_motion", "on", MON_7AM + timedelta(minutes=2)) == []
    env.engine.on_state("binary_sensor.hall_motion", "off", MON_7AM + timedelta(minutes=11))
    assert len(env.engine.on_state("binary_sensor.hall_motion", "on", MON_7AM + timedelta(minutes=12))) == 1


# Conditions -------------------------------------------------------------------------------------

def test_conditions(env):
    e, now = env.engine, MON_7AM
    env.states.update({"person.owner": "not_home", "person.guest": "home", "sensor.temp": "19", "input_boolean.quiet": "off"})
    ok = lambda *c: e.check(list(c), now)[0]  # noqa: E731
    assert ok({"type": "time", "after": "06:00", "before": "09:00"}) and not ok({"type": "time", "after": "08:00"})
    assert ok({"type": "time", "after": "22:00", "before": "08:00"})  # across midnight
    assert ok({"type": "days", "days": "weekdays"}) and not ok({"type": "days", "days": ["sat", "sun"]})
    assert ok({"type": "someone_home"}) and ok({"type": "state", "entity": "person.owner", "is_not": "home"})
    assert ok({"type": "state", "entity": "sensor.temp", "below": 20}) and not ok({"type": "state", "entity": "sensor.temp", "above": 20})
    assert ok({"type": "state", "entity": "input_boolean.quiet", "is": "on", "not": True})
    assert ok({"type": "sun", "is": "down"}) and not ok({"type": "sun", "is": "up"})  # 7:00 in late October: still dark
    assert not ok({"type": "sun", "is": "up"}, {"type": "sun", "is": "down"})
    assert e.check([{"type": "var", "name": "x", "contains": "rain"}], now, {"x": "Light rain later"})[0]


def test_failed_conditions_skip_quietly(env):
    r = env.engine.add(routine("Weekend only", [{"type": "phrase", "phrases": ["pancakes"]}],
                               conditions=[{"type": "days", "days": "weekends"}]))
    run = env.engine.run(r["id"], {"type": "phrase"}, now=MON_7AM)
    assert run.state == "skipped" and env.notes == [] and env.engine.runs(r["id"]) == []
    manual = env.engine.run(r["id"], now=MON_7AM)
    assert manual.state == "skipped" and env.engine.runs(r["id"])[0]["state"] == "skipped"  # a button press is logged


# Steps ------------------------------------------------------------------------------------------

def test_steps_device_say_wait_agent_set_if_task(env):
    r = env.engine.add(routine("Morning", steps=[
        {"device": {"domain": "light", "service": "turn_on", "area_id": "kitchen", "data": {"brightness_pct": 60}}},
        {"wait": 5},
        {"ask_agent": "weather today", "save_as": "brief"},
        {"set": {"greeting": "Good morning"}},
        {"if": [{"type": "var", "name": "brief", "contains": "weather"}],
         "then": [{"say": "{greeting}. {brief}", "room": "kitchen"}], "else": [{"say": "no weather"}]},
        {"task": "Summarise my inbox for {routine}", "mode": "plan"},
        {"notify": "Morning done"}]))
    run = env.engine.run(r["id"], now=MON_7AM)
    assert run.state == "done" and env.slept == [5.0]
    assert env.tools[0] == ("ha_call_service", {"domain": "light", "service": "turn_on", "area_id": "kitchen", "data": {"brightness_pct": 60}})
    assert env.tools[1] == ("ha_call_service", {"domain": "script", "service": "announce",
                                                "data": {"message": "Good morning. agent says: weather today", "room": "kitchen"}})
    assert run.spoken == ["Good morning. agent says: weather today"]
    assert env.tasks[0]["prompt"] == "Summarise my inbox for Morning" and env.tasks[0]["mode"] == "plan"
    assert env.tasks[0]["source"].startswith("rule:")  # task hooks never re-trigger routines from it
    assert env.notes == ["Morning done"] and [s["step"] for s in run.steps][-1] == "notify"
    assert env.engine.runs(r["id"])[0]["vars"]["brief"] == "agent says: weather today"


def test_stop_and_run_routine_and_nesting_limit(env):
    env.engine.add(routine("Lights off", steps=[{"device": {"domain": "light", "service": "turn_off", "area_id": "all"}}, {"say": "Lights off", "speaker": False}]))
    r = env.engine.add(routine("Good night", steps=[{"run_routine": "Lights off"}, {"stop": "that's all"}, {"notify": "never"}]))
    run = env.engine.run(r["id"], now=MON_7AM)
    assert run.state == "done" and run.spoken == ["Lights off"] and env.notes == []
    loop = env.engine.add(routine("Loop", steps=[{"run_routine": "Loop"}]))
    assert env.engine.run(loop["id"], now=MON_7AM).state == "failed"
    assert "nested too deeply" in env.notes[-1]


def test_failed_device_step_stops_the_run_and_tells_the_owner(env):
    env.fail_tool = True
    r = env.engine.add(routine("Broken", steps=[{"device": {"domain": "light", "service": "turn_on"}}, {"notify": "never"}]))
    run = env.engine.run(r["id"], now=MON_7AM)
    assert run.state == "failed" and env.notes == ["Routine 'Broken' failed: ha_call_service failed: broken"]


def test_dry_run_changes_nothing(env):
    r = env.engine.add(routine("Test me", steps=[{"device": {"domain": "lock", "service": "unlock"}}, {"wait": 60},
                                                 {"notify": "hi"}, {"ask_agent": "x"}, {"confirm": "sure?"}]))
    run = env.engine.run(r["id"], now=MON_7AM, dry_run=True)
    assert run.state == "done" and env.tools == [] and env.slept == [] and env.notes == [] and env.engine.get(r["id"])["last_run"] is None


def test_confirm_step_asks_the_phone(conn, phone):
    approvals = Approvals([phone.public_pem])
    env = Env(conn, approvals=approvals)

    def deny_on_first_poll(_s):
        req = approvals.pending()[0]
        approvals.decide(req.id, "deny", phone.sign(req, "deny"))
    env.engine.sleep = deny_on_first_poll
    r = env.engine.add(routine("Vacation mode", steps=[{"confirm": "Turn on vacation mode?"}, {"notify": "on"}]))
    run = env.engine.run(r["id"], now=MON_7AM)
    assert run.state == "done" and run.steps[-1] == {"step": "confirm", "result": "denied"} and env.notes == []
    assert Env(conn).engine._confirm("x", run) == "no approvals set up"


def test_runaway_routine_is_stopped(env):
    steps = [{"if": [{"type": "days"}], "then": [{"set": {"x": "y"}}] * 60}] * 5
    r = env.engine.add(routine("Big", steps=steps))
    run = env.engine.run(r["id"], now=MON_7AM)
    assert run.state == "failed" and "stopped after" in run.steps[-1]["result"]


# Through the agent: approvals and hooks still apply ---------------------------------------------

def test_device_steps_use_the_agents_tool_path_with_approvals(conn):
    ran = []
    lock = Tool("ha_call_service", "ha", {"type": "object"}, lambda i: ran.append(i) or "ok", lambda i: i.get("domain") == "lock")
    agent = HarnessAgent(router_with([Scripted("claude", [])], {"default": ["claude:m"]}), ToolRegistry([lock]), AuditLog(conn),
                         sleep=lambda s: None)
    run_tool = tool_runner(agent, Profile("assistant"))
    assert run_tool("ha_call_service", {"domain": "light", "service": "turn_on"}, "routine:x") == ("ok", False)
    text, err = run_tool("ha_call_service", {"domain": "lock", "service": "unlock"}, "routine:x")
    assert "approval" in text and len(ran) == 1  # no approvals configured: the lock is never opened
    assert run_tool("ha_call_service", {"domain": "light"}, "routine:x")[1] is False
    blocked = tool_runner(agent, Profile("kids", block=("ha_*",)))
    assert blocked("ha_call_service", {"domain": "light", "service": "turn_on"}, "routine:x")[1] is True


def test_agent_tools_list_run_and_draft(env):
    env.engine.add(routine("Good night", [{"type": "phrase", "phrases": ["good night"]}], [{"say": "Night", "speaker": False}]))
    tools = {t.name: t for t in env.engine.tools()}
    assert tools["list_routines"].handler({})[0]["when"] == '"good night" -> 1 step(s)'
    assert tools["run_routine"].handler({"name": "good night"})["said"] == ["Night"]
    assert "error" in tools["run_routine"].handler({"name": "nope"})
    out = tools["create_routine"].handler({"routine": routine("Plants", [{"type": "time", "at": "18:00"}], [{"notify": "Water the plants"}])})
    assert out["enabled"] is False and "approving it on their phone" in out["note"]
    assert tools["run_routine"].handler({"name": "Plants"}) == {"error": "that routine is switched off"}


# API ----------------------------------------------------------------------------------------------

def test_routines_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from home_agents.api import build_app
    from home_agents.settings import Settings

    monkeypatch.setenv("HOME_AGENTS_SERVICE_TOKEN", "t0k")
    monkeypatch.setenv("HOME_AGENTS_DATA", str(tmp_path))
    app = build_app(Settings(), client=object(), harness_config={"models": {"default": ["claude:m"]}},
                    providers={"claude": Scripted("claude", [])}, start_workers=False)
    c, H = TestClient(app), {"Authorization": "Bearer t0k"}
    assert c.post("/v1/h/routines", json={"name": "x", "triggers": []}, headers=H).status_code == 400
    r = c.post("/v1/h/routines", json=routine("Doorbell", [{"type": "webhook", "name": "doorbell"},
                                                           {"type": "state", "entity": "binary_sensor.door", "to": "on"}],
                                              [{"say": "Someone's at the door", "speaker": False}]), headers=H).json()
    assert c.get("/v1/h/routines", headers=H).json()[0]["summary"].startswith("webhook doorbell or binary_sensor.door")
    assert c.post(f"/v1/h/routines/{r['id']}/run", headers=H).json()["said"] == ["Someone's at the door"]
    assert c.post("/v1/h/routines/Doorbell/run?dry_run=true", headers=H).json()["state"] == "done"  # by name, for Shortcuts
    assert c.post("/v1/h/routines/webhook/doorbell", json={}, headers=H).json()["ran"][0]["state"] == "done"
    ev = {"entity_id": "binary_sensor.door", "new_state": "on", "old_state": "off"}
    assert len(c.post("/v1/h/routines/events", json=ev, headers=H).json()["ran"]) == 1
    assert len(c.get(f"/v1/h/routines/{r['id']}/runs", headers=H).json()) == 4
    c.post(f"/v1/h/routines/{r['id']}/enable?on=false", headers=H)
    assert c.post(f"/v1/h/routines/{r['id']}/run", headers=H).status_code == 409
    assert c.put(f"/v1/h/routines/{r['id']}", json={"name": "Doorbell"}, headers=H).status_code == 400
    assert c.delete(f"/v1/h/routines/{r['id']}", headers=H).json() == {"deleted": r["id"]}
    assert c.post("/v1/h/routines/rtn_nope/run", headers=H).status_code == 404
    assert c.post("/v1/h/routines/webhook/doorbell", json={}).status_code == 401
