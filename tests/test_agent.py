from conftest import FakeClient, block, response

from home_agents.agent import Agent
from home_agents.approvals import Approvals
from home_agents.audit import AuditLog
from home_agents.memory import Memory
from home_agents.models import Spend
from home_agents.tools import Tool, ToolRegistry, memory_tools


def make_agent(conn, client, tools, approvals=None):
    mem = Memory(conn)
    reg = ToolRegistry(tools + memory_tools(mem))
    return Agent(client, reg, mem, Spend(conn, 1.4, 5, 70), AuditLog(conn), approvals), mem


def test_tool_loop_and_memory(conn):
    calls = []
    lights = Tool("ha_call_service", "d", {"type": "object"}, lambda i: calls.append(i) or {"ok": True})
    client = FakeClient([
        response([block(type="tool_use", id="t1", name="ha_call_service",
                        input={"domain": "light", "service": "turn_off", "area_id": "kitchen"})], "tool_use"),
        response([block(type="text", text="Kitchen lights are off.")]),
    ])
    agent, mem = make_agent(conn, client, [lights])
    reply = agent.converse("turn off the kitchen lights", "c1", room="kitchen")
    assert reply.text == "Kitchen lights are off."
    assert calls == [{"domain": "light", "service": "turn_off", "area_id": "kitchen"}]
    # tool result went back in one user message, and the model saw the system prompt + tools
    second = client.requests[1]
    assert second["messages"][-1]["content"][0]["tool_use_id"] == "t1"
    assert second["fallbacks"] == "default"
    assert any(t.get("name") == "web_search" for t in second["tools"])
    # the conversation continues on the next turn
    client.responses.append(response([block(type="text", text="Yes.")]))
    agent.converse("did it work?", "c1")
    assert [m["role"] for m in client.requests[-1]["messages"]] == ["user", "assistant", "user"]


def test_locks_wait_for_phone_approval(conn, phone):
    calls = []
    from home_agents.tools import _ha_needs_approval
    ha = Tool("ha_call_service", "d", {"type": "object"}, lambda i: calls.append(i), _ha_needs_approval)
    client = FakeClient([
        response([block(type="tool_use", id="t1", name="ha_call_service",
                        input={"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"})], "tool_use"),
        response([block(type="text", text="I've asked for approval on your phone.")]),
    ])
    approvals = Approvals([phone.public_pem])
    agent, _ = make_agent(conn, client, [ha], approvals)
    reply = agent.converse("unlock the front door")
    assert calls == []
    assert len(reply.pending_approvals) == 1


def test_spend_is_recorded_in_cad(conn):
    client = FakeClient([response([block(type="text", text="Hi")], inp=1_000_000, out=0)])
    agent, _ = make_agent(conn, client, [])
    agent.converse("hello")
    assert round(agent.spend.month(), 2) == 2.80   # 1M Sonnet input tokens at US$2 x 1.40
