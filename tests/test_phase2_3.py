from datetime import datetime, timedelta

import anthropic
import httpx
from conftest import FakeClient

from home_agents.agent import Agent
from home_agents.audit import AuditLog
from home_agents.localllm import LocalModel
from home_agents.memory import Memory
from home_agents.models import LOCAL, Purpose, choose_model
from home_agents.models import Spend
from home_agents.routines import Action, suggest, to_automation_yaml
from home_agents.scheduler import Schedule, due, next_run
from home_agents.tools import ToolRegistry


def local_server():
    def handler(req: httpx.Request):
        if req.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Locally answered."}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    return LocalModel("http://studio:8080", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_private_requests_stay_local():
    assert choose_model(Purpose.DEFAULT, "privately, what's on my calendar", local_available=True).model == LOCAL
    assert choose_model(Purpose.DEFAULT, "what's on my calendar", local_available=True).model != LOCAL


def test_cloud_outage_falls_back_to_local(conn):
    class Down(FakeClient):
        def _create(self, **kw):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    mem = Memory(conn)
    agent = Agent(Down([]), ToolRegistry(), mem, Spend(conn, 1.4, 5, 70), AuditLog(conn), local=local_server())
    r = agent.converse("what's the capital of France")
    assert r.model == LOCAL and r.text == "Locally answered."
    assert [m["role"] for m in mem.history(r.conversation_id)] == ["user", "assistant"]


def test_routine_suggestions():
    start = datetime(2026, 9, 1, 22, 0)
    acts = [Action(start + timedelta(days=d, minutes=(d % 3) * 4), "light.living_room", "light.turn_off") for d in range(28)]
    acts += [Action(start + timedelta(days=d, hours=-12), "switch.kettle", "switch.turn_on") for d in (0, 9, 20)]
    s = suggest(acts, 28)
    assert len(s) == 1 and s[0].entity_id == "light.living_room" and s[0].days == "daily"
    assert "trigger: time" in to_automation_yaml(s[0])


def test_monthly_schedule():
    s = Schedule("ttc", "make my TTC reimbursement for last month", "monthly", day=31)
    assert next_run(s, datetime(2026, 2, 10)) == datetime(2026, 2, 28, 9, 0)
    assert due([s], {"ttc": datetime(2026, 1, 31, 9, 0)}, datetime(2026, 2, 28, 9, 1)) == [s]
