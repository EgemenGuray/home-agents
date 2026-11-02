"""Room awareness, like Alexa rooms: "turn off the lights" and "turn off the TV" mean the room you're in."""

import importlib.util
from pathlib import Path

import pytest
from conftest import FakeClient, block, response
from fastapi.testclient import TestClient

from home_agents.agent import Agent
from home_agents.audit import AuditLog
from home_agents.harness import build_harness
from home_agents.memory import Memory
from home_agents.models import Spend
from home_agents.rooms import Device, NoDeviceHere, RoomContext
from home_agents.tools import Tool, ToolRegistry, _ha_needs_approval
from test_harness import Scripted, call, say

HOUSE = [
    {"entity_id": "light.bedroom_lamp", "name": "Bedside lamp", "area": "bedroom"},
    {"entity_id": "light.bedroom_ceiling", "name": "Ceiling", "area": "bedroom"},
    {"entity_id": "media_player.bedroom_tv", "name": "TV", "area": "bedroom", "aliases": ["telly"]},
    {"entity_id": "media_player.bedroom_voice", "name": "Bedroom voice", "area": "bedroom", "here": True},
    {"entity_id": "light.living_room_lamp", "name": "Floor lamp", "area": "living_room"},
    {"entity_id": "media_player.living_room_tv", "name": "TV", "area": "living_room"},
    {"entity_id": "media_player.tv_and_sound", "name": "TV and sound", "area": "living_room",
     "members": ["media_player.living_room_tv", "media_player.living_room_soundbar"]},
    {"entity_id": "cover.garage_door", "name": "Garage door", "area": "garage"},
    {"entity_id": "switch.windows_pc", "name": "Windows PC"},
]


def bedroom():
    return RoomContext.from_request("bedroom", "upstairs", HOUSE)


# What a device command with no target means ----------------------------------------------------------

def test_the_lights_means_this_rooms_lights():
    out = bedroom().target({"domain": "light", "service": "turn_off"})
    assert out["entity_id"] == ["light.bedroom_lamp", "light.bedroom_ceiling"]


def test_the_tv_means_this_rooms_tv_not_the_speaker_itself():
    out = bedroom().target({"domain": "media_player", "service": "turn_off"})
    assert out["entity_id"] == ["media_player.bedroom_tv"]


def test_the_same_words_in_another_room():
    ctx = RoomContext.from_request("living_room", None, HOUSE)
    # The group is its own entity: "turn off the TV" never turns off the whole group.
    assert ctx.target({"domain": "media_player", "service": "turn_off"})["entity_id"] == ["media_player.living_room_tv"]


@pytest.mark.parametrize("named", [
    {"entity_id": "media_player.living_room_tv"},
    {"area_id": "kitchen"},
    {"floor_id": "downstairs"},
    {"label_id": "movie"},
    {"data": {"entity_id": "light.living_room_lamp"}},
])
def test_a_named_target_overrides_this_room(named):
    inp = {"domain": "light", "service": "turn_off", **named}
    assert bedroom().target(inp) == inp


def test_scripts_and_scenes_are_never_room_scoped():
    inp = {"domain": "script", "service": "announce", "data": {"message": "dinner"}}
    assert bedroom().target(inp) == inp


def test_no_such_device_here_asks_instead_of_acting_on_the_house():
    with pytest.raises(NoDeviceHere, match="no vacuum in the bedroom"):
        bedroom().target({"domain": "vacuum", "service": "start"})


def test_without_a_room_nothing_is_filled_in():
    ctx = RoomContext.from_request(None, None, HOUSE)
    assert ctx.target({"domain": "light", "service": "turn_off"}) == {"domain": "light", "service": "turn_off"}


def test_without_the_device_map_home_assistant_resolves_the_area():
    ctx = RoomContext.from_request("kitchen")
    assert ctx.target({"domain": "light", "service": "turn_off"})["area_id"] == "kitchen"


def test_approval_sees_the_device_that_will_change():
    garage = RoomContext.from_request("garage", None, HOUSE)
    inp = garage.wrap(Tool("ha_call_service", "", {}, lambda i: i, _ha_needs_approval)).prepared(
        {"domain": "cover", "service": "open_cover"})
    assert inp["entity_id"] == ["cover.garage_door"] and _ha_needs_approval(inp)


# What the model is told ------------------------------------------------------------------------------

def test_prompt_lists_this_room_first_with_groups_and_the_rule():
    text = bedroom().prompt()
    assert "means the one in this room" in text and "This room: bedroom (floor upstairs)." in text
    lines = text.splitlines()
    rooms = [line.split(":")[0] for line in lines if line.startswith("- ")]
    assert rooms == ["- bedroom (this room)", "- garage", "- living_room", "- no room"]
    assert "TV / telly (media_player.bedroom_tv)" in text
    assert "group of media_player.living_room_tv, media_player.living_room_soundbar" in text


def test_typed_chat_without_room_or_devices_adds_nothing():
    assert RoomContext.from_request(None, None, None).prompt() == ""


def test_bad_rows_are_dropped():
    ctx = RoomContext.from_request("bedroom", None, [{"name": "no id"}, "junk", {"entity_id": "light.x"}])
    assert ctx.devices == [Device("light.x", "x")]


# Through the harness (voice and app) and the agent ----------------------------------------------------

def harness(conn, tmp_path, results):
    calls = []
    ha = Tool("ha_call_service", "", {"type": "object"}, lambda i: calls.append(i) or {"ok": True}, _ha_needs_approval)
    p = Scripted("claude", results)
    spend = Spend(conn, 1.4, 5, 70)
    h = build_harness({"models": {"default": ["claude:m"]}, "routines": False}, conn=conn, data_dir=tmp_path,
                      audit=AuditLog(conn), memory=Memory(conn), spend=spend, approvals=None,
                      base_tools=ToolRegistry([ha]), providers={"claude": p}, start_workers=False)
    return h, p, calls


def test_voice_in_the_bedroom_turns_off_the_bedroom_lights(conn, tmp_path):
    h, p, calls = harness(conn, tmp_path, [call("ha_call_service", {"domain": "light", "service": "turn_off"}), say("Done.")])
    _, r = h.converse("turn off the lights", "c1", channel="voice", room="bedroom", floor="upstairs", devices=HOUSE)
    assert r.text == "Done."
    assert calls == [{"domain": "light", "service": "turn_off", "entity_id": ["light.bedroom_lamp", "light.bedroom_ceiling"]}]
    assert "Devices by room:" in p.calls[0]["system"] and "This room: bedroom" in p.calls[0]["system"]


def test_harness_returns_the_missing_device_to_the_model(conn, tmp_path):
    h, p, calls = harness(conn, tmp_path, [call("ha_call_service", {"domain": "vacuum", "service": "start"}),
                                           say("There's no vacuum in here. Which room?")])
    h.converse("start the vacuum", "c2", channel="voice", room="bedroom", devices=HOUSE)
    assert calls == []
    tool_msg = p.calls[1]["messages"][-1]
    assert tool_msg["is_error"] and "Ask the user which room" in tool_msg["content"]


def test_harness_approval_for_a_room_target(conn, tmp_path):
    h, p, calls = harness(conn, tmp_path, [call("ha_call_service", {"domain": "cover", "service": "open_cover"}), say("Waiting.")])
    h.converse("open the door", "c3", channel="voice", room="garage", devices=HOUSE)
    assert calls == [] and "needs the owner's approval" in p.calls[1]["messages"][-1]["content"]


def test_legacy_agent_uses_the_room_too(conn):
    calls = []
    ha = Tool("ha_call_service", "", {"type": "object"}, lambda i: calls.append(i) or {"ok": True})
    client = FakeClient([
        response([block(type="tool_use", id="t1", name="ha_call_service", input={"domain": "media_player", "service": "turn_off"})],
                 stop_reason="tool_use"),
        response([block(type="text", text="TV's off.")]),
    ])
    agent = Agent(client, ToolRegistry([ha]), Memory(conn), Spend(conn, 1.4, 5, 70), AuditLog(conn))
    r = agent.converse("turn off the TV", channel="voice", room="bedroom", room_context=bedroom())
    assert r.text == "TV's off." and calls[0]["entity_id"] == ["media_player.bedroom_tv"]
    assert "Devices by room:" in client.requests[0]["system"][1]["text"]


def test_api_passes_room_and_devices_from_home_assistant(tmp_path, monkeypatch):
    from home_agents.api import build_app
    from home_agents.settings import Settings

    monkeypatch.setenv("HOME_AGENTS_SERVICE_TOKEN", "t0k")
    monkeypatch.setenv("HOME_AGENTS_DATA", str(tmp_path))
    p = Scripted("claude", [say("Which TV?")])
    app = build_app(Settings(), client=object(), harness_config={"models": {"default": ["claude:m"]}},
                    providers={"claude": p}, start_workers=False)
    body = {"text": "turn off the TV", "conversation_id": "c9", "channel": "voice", "room": "bedroom",
            "floor": "upstairs", "devices": HOUSE}
    r = TestClient(app).post("/v1/converse", json=body, headers={"Authorization": "Bearer t0k"})
    assert r.status_code == 200
    assert "TV / telly (media_player.bedroom_tv)" in p.calls[0]["system"]


# The HA integration's side (no Home Assistant needed) -------------------------------------------------

def _logic():
    path = Path(__file__).parents[1] / "ha_integration/custom_components/home_agent/logic.py"
    spec = importlib.util.spec_from_file_location("home_agent_logic_rooms", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_integration_rows_prefer_the_entitys_own_area():
    logic = _logic()
    assert logic.device_row("light.a", "A", "office", "bedroom")["area"] == "office"
    assert logic.device_row("light.a", "A", None, "bedroom")["area"] == "bedroom"
    row = logic.device_row("media_player.g", "G", None, None, aliases={"b", "a"}, members=["media_player.x"], here=True)
    assert row == {"entity_id": "media_player.g", "name": "G", "area": None, "aliases": ["a", "b"],
                   "members": ["media_player.x"], "here": True}
    assert "members" not in logic.device_row("light.a", "A", None, None, members="light.a")


def test_integration_sends_this_room_first_and_caps_the_list():
    logic = _logic()
    rows = [logic.device_row(f"light.l{i}", "L", "kitchen", None) for i in range(400)]
    rows += [logic.device_row("light.z", "Z", "bedroom", None), logic.device_row("switch.pc", "PC", None, None)]
    out = logic.house_devices(rows, "bedroom")
    assert out[0]["entity_id"] == "light.z" and len(out) == logic.MAX_DEVICES
    assert logic.house_devices(rows[-1:] + rows[:1], None)[0]["entity_id"] == "light.l0"  # no room: rooms before "no room"
