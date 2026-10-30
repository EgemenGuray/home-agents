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
