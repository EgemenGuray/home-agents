"""Event hooks (E1.12): Claude Code's and Kiro's hooks and CrewAI's callbacks, as household rules.

Rules live in harness.yaml, a protected file only the owner edits:

    event_hooks:
        - on: tool_before            # tool_before, tool_after, task_done, task_failed, memory_sleep
          match: {tool: "ha_call_service", input_contains: "oven"}
          do: ask                    # ask (phone approval), block, notify, or task
        - on: task_failed
          do: notify
          message: "Task failed: {title}"
        - on: task_done
          match: {profile: researcher}
          do: task
          prompt: "Turn this research into a one-paragraph brief: {result}"

A hook can make things stricter (block, ask) or start follow-up work; it never runs shell
commands on the host. Templates fill {placeholders} from the event, and missing keys stay empty.
"""

from __future__ import annotations

import fnmatch
import json
import string
from typing import Callable

EVENTS = ("tool_before", "tool_after", "task_done", "task_failed", "memory_sleep")
ACTIONS = ("ask", "block", "notify", "task")


class _Blank(dict):
    def __missing__(self, key):
        return ""


def fill(template: str, ctx: dict) -> str:
    safe = {k: (v if isinstance(v, str) else json.dumps(v, default=str))[:4000] for k, v in ctx.items()}
    return string.Formatter().vformat(template, (), _Blank(safe))


class EventHooks:
    def __init__(self, rules: list[dict] | None = None, notify: Callable[[str], None] = lambda text: None,
                 start_task: Callable[[str, str, str], None] = lambda prompt, profile, title: None, audit=None):
        self.rules = []
        for r in rules or []:
            if True in r:  # YAML 1.1 reads a bare `on:` key as true
                r = {("on" if k is True else k): v for k, v in r.items()}
            if r.get("on") not in EVENTS or r.get("do") not in ACTIONS:
                raise ValueError(f"bad hook rule {r}")
            if r["do"] in ("ask", "block") and r["on"] != "tool_before":
                raise ValueError("ask and block only make sense on tool_before")
            self.rules.append(r)
        self.notify = notify
        self.start_task = start_task
        self.audit = audit
        self.listeners: list[Callable[[str, dict], None]] = []  # e.g. routines with a harness trigger

    def _matches(self, rule: dict, ctx: dict) -> bool:
        m = rule.get("match") or {}
        if "tool" in m and not fnmatch.fnmatch(ctx.get("tool", ""), m["tool"]):
            return False
        if "profile" in m and not fnmatch.fnmatch(ctx.get("profile", ""), m["profile"]):
            return False
        if "input_contains" in m and m["input_contains"].lower() not in json.dumps(ctx.get("input", {})).lower():
            return False
        if "result_contains" in m and m["result_contains"].lower() not in str(ctx.get("result", "")).lower():
            return False
        return True

    def emit(self, event: str, ctx: dict) -> str | None:
        """Runs every matching rule. Returns "block" or "ask" for tool_before when a rule says so."""
        verdict = None
        if event in ("task_done", "task_failed") and str(ctx.get("source", "")).startswith("rule:"):
            return None  # work a hook started never triggers more hooks, so rules can't loop
        if event in ("task_done", "task_failed", "memory_sleep"):
            for listener in self.listeners:
                listener(event, ctx)
        for rule in self.rules:
            if rule["on"] != event or not self._matches(rule, ctx):
                continue
            do = rule["do"]
            if self.audit:
                self.audit.record("hook_fired", "hooks", {"event": event, "do": do, "tool": ctx.get("tool")}, ctx.get("task_id"))
            if do == "block":
                verdict = "block"
            elif do == "ask" and verdict != "block":
                verdict = "ask"
            elif do == "notify":
                self.notify(fill(rule.get("message", f"{event}: {{title}}"), ctx))
            elif do == "task":
                self.start_task(fill(rule["prompt"], ctx), rule.get("profile", "assistant"), rule.get("title", f"Hook: {event}"))
        return verdict

    def before_tool(self, tool: str, inp: dict, profile: str) -> str | None:
        return self.emit("tool_before", {"tool": tool, "input": inp, "profile": profile})

    def after_tool(self, tool: str, inp: dict, result: str, profile: str) -> None:
        self.emit("tool_after", {"tool": tool, "input": inp, "result": result, "profile": profile})
