"""Music by room: "play music" plays on the music speaker of the room you're in (Spotify via Music Assistant,
on AirPlay and other players), never the TV and never the whole house."""

import pytest
from test_harness import call, say
from test_rooms import _logic, harness

from home_agents.rooms import Device, NoDeviceHere, RoomContext

HOUSE = [
    {"entity_id": "media_player.living_room_tv", "name": "TV", "area": "living_room"},
    {"entity_id": "media_player.living_room_bo", "name": "Beosound", "area": "living_room", "music": True},
    {"entity_id": "media_player.living_room_voice", "name": "Living room voice", "area": "living_room", "here": True},
    {"entity_id": "media_player.kitchen_voice", "name": "Kitchen voice", "area": "kitchen", "here": True},
    {"entity_id": "media_player.kitchen_tv", "name": "TV", "area": "kitchen"},
    {"entity_id": "light.office_lamp", "name": "Lamp", "area": "office"},
]


def room(name):
    return RoomContext.from_request(name, None, HOUSE)


@pytest.mark.parametrize("service", [("music_assistant", "play_media"), ("media_player", "play_media")])
def test_play_with_no_target_goes_to_this_rooms_music_speaker_not_the_tv(service):
    domain, svc = service
    out = room("living_room").target({"domain": domain, "service": svc, "data": {"media_id": "Daft Punk"}})
    assert out["entity_id"] == ["media_player.living_room_bo"]


def test_a_room_without_a_music_speaker_plays_on_its_voice_device():
    out = room("kitchen").target({"domain": "music_assistant", "service": "play_media", "data": {"media_id": "jazz"}})
    assert out["entity_id"] == ["media_player.kitchen_voice"]


def test_no_speaker_in_this_room_asks_instead_of_playing_everywhere():
    with pytest.raises(NoDeviceHere, match="no speaker for music in the office"):
        room("office").target({"domain": "music_assistant", "service": "play_media", "data": {"media_id": "jazz"}})


def test_a_named_speaker_overrides_this_room():
    inp = {"domain": "music_assistant", "service": "play_media", "entity_id": "media_player.kitchen_voice",
           "data": {"media_id": "jazz"}}
    assert room("living_room").target(inp) == inp


def test_other_media_commands_still_mean_the_tv():
    out = room("living_room").target({"domain": "media_player", "service": "turn_off"})
    assert "media_player.living_room_tv" in out["entity_id"] and "media_player.living_room_voice" not in out["entity_id"]


@pytest.mark.parametrize("script", ["play_music", "stop_music"])
def test_the_music_scripts_get_this_room_when_none_is_named(script):
    out = room("kitchen").target({"domain": "script", "service": script, "data": {"query": "jazz"}})
    assert out["data"] == {"query": "jazz", "rooms": "kitchen"}


@pytest.mark.parametrize("rooms", ["bedroom", "downstairs", "everywhere", ["kitchen", "bedroom"]])
def test_a_named_room_floor_or_everywhere_is_kept(rooms):
    inp = {"domain": "script", "service": "play_music", "data": {"rooms": rooms}}
    assert room("kitchen").target(inp) == inp


def test_typed_chat_has_no_room_so_nothing_is_filled_in():
    inp = {"domain": "script", "service": "play_music", "data": {"query": "jazz"}}
    assert RoomContext.from_request(None, None, HOUSE).target(inp) == inp


def test_without_the_device_map_home_assistant_resolves_the_area():
    out = RoomContext.from_request("kitchen").target({"domain": "media_player", "service": "play_media"})
    assert out["area_id"] == "kitchen"


def test_prompt_marks_the_music_speaker_and_tells_the_model_how_to_play():
    text = room("living_room").prompt()
    assert "Beosound (media_player.living_room_bo, music speaker)" in text
    assert "script.play_music" in text and "everywhere" in text


def test_the_flag_comes_from_the_home_assistant_row():
    assert Device.parse({"entity_id": "media_player.x", "music": True}).music
    assert not Device.parse({"entity_id": "media_player.x"}).music


def test_voice_in_the_kitchen_plays_in_the_kitchen(conn, tmp_path):
    h, p, calls = harness(conn, tmp_path, [
        call("ha_call_service", {"domain": "script", "service": "play_music", "data": {"query": "something calm"}}),
        say("Playing something calm.")])
    _, r = h.converse("put on something calm for dinner", "m1", channel="voice", room="kitchen", devices=HOUSE)
    assert r.text == "Playing something calm."
    assert calls == [{"domain": "script", "service": "play_music",
                      "data": {"query": "something calm", "rooms": "kitchen"}}]


def test_integration_marks_the_music_speaker():
    logic = _logic()
    assert logic.MUSIC_LABEL == "music"
    assert logic.device_row("media_player.bo", "B&O", "living_room", None, music=True)["music"] is True
    assert "music" not in logic.device_row("media_player.tv", "TV", "living_room", None)
