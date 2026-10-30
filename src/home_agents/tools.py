"""Tools the agent can call (S1.2.1). Home Assistant is reached only through a scoped user.

Each tool declares whether a call needs your approval; the agent loop enforces it,
so a prompt cannot talk its way past it (S0.5.9, S1.6.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

# Domains whose actions are irreversible or security-relevant: always ask first.
APPROVAL_DOMAINS = {"lock", "alarm_control_panel", "button"}
APPROVAL_ENTITY_HINTS = ("garage",)


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]
    needs_approval: Callable[[dict], bool] = field(default=lambda _input: False)
    strict: bool = False  # only for schemas with no free-form objects

    def definition(self) -> dict:
        d = {"name": self.name, "description": self.description, "input_schema": self.input_schema}
        if self.strict:
            d["strict"] = True
        return d


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.add(t)

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[dict]:
        return [t.definition() for t in sorted(self._tools.values(), key=lambda t: t.name)]  # stable order keeps the cache warm


def _ha_needs_approval(inp: dict) -> bool:
    if inp.get("domain") in APPROVAL_DOMAINS:
        return True
    targets = " ".join(str(v) for v in (inp.get("entity_id"), inp.get("area_id")) if v)
    return any(h in targets for h in APPROVAL_ENTITY_HINTS) and inp.get("domain") == "cover"


class HomeAssistant:
    """Thin REST client; the token belongs to a non-admin HA user limited to exposed entities."""

    def __init__(self, url: str, token: str, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.http = client or httpx.Client(timeout=10, headers={"Authorization": f"Bearer {token}"})

    def call_service(self, inp: dict) -> dict:
        data = dict(inp.get("data") or {})
        for key in ("entity_id", "area_id"):
            if inp.get(key):
                data[key] = inp[key]
        r = self.http.post(f"{self.url}/api/services/{inp['domain']}/{inp['service']}", json=data)
        r.raise_for_status()
        return {"ok": True, "changed": [s.get("entity_id") for s in r.json()][:20]}

    def states(self, inp: dict) -> list[dict]:
        r = self.http.get(f"{self.url}/api/states")
        r.raise_for_status()
        prefix = inp.get("domain") or ""
        area_hint = (inp.get("contains") or "").lower()
        out = []
        for s in r.json():
            eid = s["entity_id"]
            name = s.get("attributes", {}).get("friendly_name", eid)
            if prefix and not eid.startswith(prefix + "."):
                continue
            if area_hint and area_hint not in (eid + " " + name).lower():
                continue
            out.append({"entity_id": eid, "name": name, "state": s["state"]})
        return out[:60]

    def snapshot(self) -> dict[str, str]:
        """Every exposed entity's state, for routine triggers and conditions."""
        r = self.http.get(f"{self.url}/api/states")
        r.raise_for_status()
        return {s["entity_id"]: s["state"] for s in r.json()}

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "ha_call_service",
                "Control a device or run a script or scene in Home Assistant, e.g. light.turn_on for an area.",
                {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string"},
                        "service": {"type": "string"},
                        "entity_id": {"type": "string"},
                        "area_id": {"type": "string"},
                        "data": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["domain", "service"],
                    "additionalProperties": False,
                },
                self.call_service,
                _ha_needs_approval,
            ),
            Tool(
                "ha_states",
                "Read current device states, optionally filtered by domain (light, sensor...) or text in the name.",
                {
                    "type": "object",
                    "properties": {"domain": {"type": "string"}, "contains": {"type": "string"}},
                    "required": [],
                    "additionalProperties": False,
                },
                self.states,
            ),
        ]


def memory_tools(memory) -> list[Tool]:
    return [
        Tool(
            "remember_fact",
            "Store a household fact or preference the user told you to remember.",
            {
                "type": "object",
                "properties": {"subject": {"type": "string"}, "fact": {"type": "string"}},
                "required": ["subject", "fact"],
                "additionalProperties": False,
            },
            lambda i: {"id": memory.remember(i["subject"], i["fact"])},
            strict=True,
        ),
        Tool(
            "forget_fact",
            "Forget stored facts containing this text, when the user says 'forget that'.",
            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
            lambda i: {"forgotten": memory.forget_matching(i["text"])},
            strict=True,
        ),
    ]


def result_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)[:20000]
