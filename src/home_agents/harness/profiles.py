"""Agent profiles (E1.12): a persona, a model chain, skills, and a stance for every tool.

Stances follow Cowork's allow / ask / block. "block" hides the tool, "ask" always needs your
approval, "allow" keeps the tool's own rule. A stance can make a tool stricter, never looser:
a lock still asks even if a profile says allow, because that rule lives in the tool's code.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field, replace

from ..tools import Tool, ToolRegistry


@dataclass(frozen=True)
class Profile:
    name: str
    model: str = "default"                 # a chain alias from harness.yaml
    persona: str = ""
    allow: tuple[str, ...] = ("*",)
    ask: tuple[str, ...] = ()
    block: tuple[str, ...] = ()
    skills: tuple[str, ...] = ("*",)
    private: bool = False                  # local models only
    route: str | None = None               # egress route for its workspaces
    max_iterations: int = 20
    token_budget: int = 200_000
    extra: dict = field(default_factory=dict)

    def stance(self, tool_name: str) -> str:
        match = lambda pats: any(fnmatch.fnmatch(tool_name, p) for p in pats)
        if match(self.block):
            return "block"
        if match(self.ask):
            return "ask"
        return "allow" if match(self.allow) else "block"

    def tools(self, registry: ToolRegistry, extra: list[Tool] = ()) -> ToolRegistry:
        out = ToolRegistry()
        for t in list(registry._tools.values()) + list(extra):
            stance = self.stance(t.name)
            if stance == "block":
                continue
            if stance == "ask":
                t = replace(t, needs_approval=lambda _inp: True)
            out.add(t)
        return out


DEFAULT = Profile("assistant")


def load_profiles(config: dict) -> dict[str, Profile]:
    out = {"assistant": DEFAULT}
    for name, p in (config or {}).items():
        tools = p.get("tools") or {}
        out[name] = Profile(
            name=name, model=p.get("model", "default"), persona=p.get("persona", ""),
            allow=tuple(tools.get("allow", ["*"])), ask=tuple(tools.get("ask", [])), block=tuple(tools.get("block", [])),
            skills=tuple(p.get("skills", ["*"])), private=bool(p.get("private", False)), route=p.get("route"),
            max_iterations=int(p.get("max_iterations", 20)), token_budget=int(p.get("token_budget", 200_000)),
        )
    return out
