"""Plain decisions for the conversation agent, kept free of Home Assistant imports so they are unit tested."""

from __future__ import annotations

# Voice gives up sooner than typed chat: a quick "can't right now" beats a long silence at the speaker.
VOICE_TIMEOUT_S = 30
TEXT_TIMEOUT_S = 90

UNREACHABLE = "I can't reach the assistant right now. Lights, scenes, timers and alarms still work."
TOO_SLOW = "That's taking too long. Try again in a moment, or ask in the app."


def timeout_for(channel: str) -> int:
    return VOICE_TIMEOUT_S if channel == "voice" else TEXT_TIMEOUT_S


def local_handled(response_type: str, error_code: str | None) -> bool:
    """HA's own intents answered: a device command, a timer, a sentence trigger. Anything else goes to the agent,
    including "no device by that name", which the agent can often resolve from the room and the states."""
    return response_type != "error" and not error_code


def keep_listening(text: str) -> bool:
    """The assistant asked something back, so the speaker listens again without a second wake word."""
    return text.rstrip().endswith("?")


# Room awareness: the agent gets the exposed devices with their rooms, so "turn off the TV" said in the
# bedroom means the bedroom TV (home_agents/rooms.py). Capped so a big house doesn't bloat every request.
MAX_DEVICES = 300


def device_row(entity_id: str, name: str, entity_area: str | None, device_area: str | None,
               aliases=(), members=None, here: bool = False) -> dict:
    """One exposed entity for the agent. The entity's own area wins over its device's, as in HA."""
    row = {"entity_id": entity_id, "name": name, "area": entity_area or device_area}
    if aliases:
        row["aliases"] = sorted(str(a) for a in aliases)
    if isinstance(members, (list, tuple)) and members:
        row["members"] = [str(m) for m in members]
    if here:
        row["here"] = True
    return row


def house_devices(rows: list[dict], room: str | None) -> list[dict]:
    """This room first, then by room and entity id, so the list is stable and the cap drops far rooms."""
    ordered = sorted(rows, key=lambda r: (r.get("area") != room or room is None, r.get("area") is None,
                                          r.get("area") or "", r["entity_id"]))
    return ordered[:MAX_DEVICES]
