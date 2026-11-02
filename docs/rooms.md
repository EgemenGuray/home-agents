# Rooms and groups (like Alexa)

Said to a speaker in the bedroom, "turn off the lights" turns off the bedroom lights and "turn off the TV" the bedroom TV. Naming a room, a floor or a group ("the kitchen lights", "everything downstairs", "TV and sound") overrides that.

## Who handles what

1. **Home Assistant first.** The `home_agent` integration lets HA's own intents try every request. HA knows the speaker's area and prefers it, so simple room commands are answered locally in under a second, with no cloud and no agent (home-assistant-config `tests/test_rooms.py`).
2. **The agent for the rest** ("it's too bright in here", "turn off everything except the lamp"). The integration sends the speaker's room and floor, and every device exposed to Assist with its room, aliases, group members and whether it belongs to the speaker itself. The agent gets
   - a map of the house in its prompt, this room first, and the rule: a device named without a room is the one in this room; if this room has none and the house has one, use it; otherwise ask which room;
   - a guard in code (`rooms.py`): an `ha_call_service` device command with no target goes to this room's devices of that kind, never the whole house and never the speaker itself. A room with no such device returns an error, so the model asks. The guard runs before the approval check, so the phone shows the exact device.

Typed chat in the app or Telegram has no room, so nothing is filled in; the agent asks or uses the room you name.

## Groups

Groups live in Home Assistant, so they work from voice, the app and the agent alike, and keep working when the agent is down. Make one as a group helper in HA (Settings > Devices > Helpers > Group) or in home-assistant-config `packages/groups.yaml`, then expose it to Assist. Floors group rooms. The agent can also target an HA label (`label_id`) for looser sets such as "all the Christmas lights".

## Mac mini only, and Mac mini + Mac Studio

| | **Mac mini only** | **Mac mini + Mac Studio** |
|---|---|---|
| Rooms, floors, groups | HA on the hub Mac | The same |
| Room commands HA understands | HA on the hub Mac, locally | The same |
| Everything else | The agent service on the hub Mac, with the room context | The agent service on the Studio, with the same room context |
| If the agent service is down | Room commands, floors and groups still work through HA | The same |
| Install | Update the `home_agent` integration on HA and the agent service; no settings | The same, on the Studio |
