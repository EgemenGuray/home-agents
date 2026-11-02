"""Room awareness, like Alexa rooms: what "turn off the lights" and "turn off the TV" mean where you are.

Home Assistant's own intents already do this for simple commands (the speaker's area is the default).
This is for what reaches the agent: the HA integration sends the speaker's room and the exposed devices
with their rooms, and the agent gets

- a short map of the house in its prompt, this room first, groups with their members, and the rule
  for a device named without a room;
- a guard in code on `ha_call_service`: a device command with no target goes to this room's devices of
  that kind, never to the whole house, and a room without one is an error the model has to ask about.

The guard is code so a prompt can't talk its way past it, the same as approvals.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .tools import Tool

# Device kinds that "turn off the X" can mean. Scripts, scenes and notify calls are never room-scoped.
ROOM_DOMAINS = frozenset({"light", "switch", "fan", "cover", "media_player", "climate", "vacuum", "remote",
                          "humidifier", "water_heater", "valve"})
TARGET_KEYS = ("entity_id", "area_id", "floor_id", "label_id", "device_id")
MAX_DEVICES = 300  # keeps the prompt small in a big house; the map lists this room first

RULES = ("Rooms: a device named without a room (\"the TV\", \"the lights\") means the one in this room. "
         "If this room has none and the house has exactly one, use it; otherwise ask which room. "
         "A room, floor or group the user names overrides this room. A group is controlled by its own entity.")


class NoDeviceHere(ValueError):
    """The command has no target and this room has no device of that kind."""


@dataclass(frozen=True)
class Device:
    entity_id: str
    name: str
    area: str | None = None
    aliases: tuple[str, ...] = ()
    members: tuple[str, ...] = ()   # a group's entities
    here: bool = False              # part of the speaker that heard the request (its own media player)

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @classmethod
    def parse(cls, raw: dict) -> Device | None:
        eid = str(raw.get("entity_id") or "")
        if "." not in eid:
            return None
        return cls(eid, str(raw.get("name") or eid.split(".", 1)[1].replace("_", " ")), raw.get("area") or None,
                   tuple(str(a) for a in raw.get("aliases") or ()), tuple(str(m) for m in raw.get("members") or ()),
                   bool(raw.get("here")))

    def label(self) -> str:
        names = " / ".join([self.name, *self.aliases])
        extra = f", group of {', '.join(self.members)}" if self.members else ""
        return f"{names} ({self.entity_id}{extra})"


@dataclass
class RoomContext:
    room: str | None = None
    floor: str | None = None
    devices: list[Device] = field(default_factory=list)

    @classmethod
    def from_request(cls, room: str | None, floor: str | None = None, devices: list[dict] | None = None) -> RoomContext:
        parsed = [d for d in (Device.parse(r) for r in (devices or [])[:MAX_DEVICES] if isinstance(r, dict)) if d]
        return cls(room or None, floor or None, parsed)

    def in_room(self, domain: str, room: str | None = None) -> list[Device]:
        room = room or self.room
        return [d for d in self.devices if d.area == room and d.domain == domain and not d.members and not d.here]

    def prompt(self) -> str:
        if not self.room and not self.devices:
            return ""
        lines = [RULES]
        if self.room:
            lines.append(f"This room: {self.room}" + (f" (floor {self.floor})." if self.floor else "."))
        if self.devices:
            by_area: dict[str, list[Device]] = {}
            for d in self.devices:
                by_area.setdefault(d.area or "no room", []).append(d)
            order = sorted(by_area, key=lambda a: (a != self.room, a == "no room", a))
            lines.append("Devices by room:")
            for area in order:
                mark = " (this room)" if area == self.room else ""
                lines.append(f"- {area}{mark}: " + "; ".join(d.label() for d in by_area[area]))
        return "\n".join(lines)

    def target(self, inp: dict) -> dict:
        """The tool input with this room filled in when a device command names no target."""
        domain = inp.get("domain")
        data = inp.get("data") or {}
        if domain not in ROOM_DOMAINS or not self.room:
            return inp
        if any(inp.get(k) or data.get(k) for k in TARGET_KEYS):
            return inp
        if not self.devices:  # no map from the integration: let HA resolve the area
            return {**inp, "area_id": self.room}
        found = self.in_room(domain)
        if not found:
            raise NoDeviceHere(f"There is no {domain.replace('_', ' ')} in the {self.room.replace('_', ' ')}. "
                               "Ask the user which room they mean; do not act on the whole house.")
        return {**inp, "entity_id": [d.entity_id for d in found]}

    def wrap(self, tool: Tool) -> Tool:
        """`ha_call_service` with this room as the default target. The loop fills it in before the approval
        check, so the phone shows the device that will actually change."""
        return replace(tool, prepare=self.target)
