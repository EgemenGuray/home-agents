"""Orchestrator (S1.5.1, S2.5.1): plans a request into subtasks and runs subagents.

Each subtask gets a workspace type, an egress route, allowed peers and a model. The job
won't start a subtask whose route check fails. Spin-off is bounded by a per-request budget.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .egress import RouteMismatch, verify_route
from .models import OPUS


class State(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting_for_you"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    KILLED = "killed"


@dataclass
class Subtask:
    title: str
    instructions: str
    workspace: str = "cli"          # cli, desktop, macos (Phase 2)
    route: str = "home"
    peers: list[str] = field(default_factory=list)
    model: str = "claude-sonnet-5-5"
    agent: str = ""


@dataclass
class SpinOffBudget:
    max_subagents: int = 3          # Phase 1: 3; Phase 2: up to 20
    max_minutes: int = 60
    max_tokens: int = 400_000
    max_macos: int = 0              # Phase 2: at most 2


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "instructions": {"type": "string"},
                    "workspace": {"type": "string", "enum": ["cli", "desktop", "macos"]},
                    "route": {"type": "string"},
                    "peers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "instructions", "workspace", "route", "peers"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["subtasks"],
    "additionalProperties": False,
}

PLANNER_PROMPT = """Split the owner's request into the fewest subtasks that can run independently.
Prefer APIs and CLI work; use a desktop workspace only for websites or apps with no API. Pick a route
from: {routes}. Use "home" for any site where the owner is logged in. Never plan purchases, money
transfers or security-setting changes. Name peers only when two subtasks must exchange results."""


class Orchestrator:
    def __init__(self, conn: sqlite3.Connection, client, routes: dict, workspaces, lookup, audit=None,
                 budget: SpinOffBudget | None = None):
        self.conn = conn
        self.client = client
        self.routes = routes
        self.workspaces = workspaces
        self.lookup = lookup          # route -> (public_ip, country), through that route
        self.audit = audit
        self.budget = budget or SpinOffBudget()

    def plan(self, request: str) -> list[Subtask]:
        resp = self.client.messages.create(
            model=OPUS,
            max_tokens=8000,
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
            system=PLANNER_PROMPT.format(routes=", ".join(sorted(self.routes))),
            messages=[{"role": "user", "content": request}],
        )
        text = next(b.text for b in resp.content if b.type == "text")
        subtasks = [Subtask(**s) for s in json.loads(text)["subtasks"]]
        return self.enforce_budget(subtasks)

    def enforce_budget(self, subtasks: list[Subtask]) -> list[Subtask]:
        if len(subtasks) > self.budget.max_subagents:
            raise ValueError(f"plan needs {len(subtasks)} subagents; budget allows {self.budget.max_subagents}")
        if sum(s.workspace == "macos" for s in subtasks) > self.budget.max_macos:
            raise ValueError("plan needs more macOS workspaces than allowed")
        for i, s in enumerate(subtasks):
            if s.route not in self.routes:
                raise ValueError(f"unknown route {s.route!r}")
            s.agent = s.agent or f"agent{i + 1}"
        return subtasks

    def peer_pairs(self, subtasks: list[Subtask]) -> list[list[str]]:
        """Allow-list entries for this job: orchestrator and you talk to all; peers only as planned."""
        by_title = {s.title: s.agent for s in subtasks}
        pairs = [["orchestrator", "*"], ["human", "*"]]
        for s in subtasks:
            for p in s.peers:
                if p in by_title:
                    pairs.append([s.agent, by_title[p]])
        return pairs

    def start(self, request: str, subtasks: list[Subtask]) -> str:
        job_id = f"job-{uuid.uuid4().hex[:10]}"
        self._save(job_id, None, request[:200], State.RUNNING)
        for s in subtasks:
            sub_id = f"{job_id}-{s.agent}"
            try:
                check = verify_route(s.route, self.routes, self.lookup)
            except RouteMismatch as exc:
                self._save(sub_id, job_id, s.title, State.FAILED, s, result=str(exc))
                if self.audit:
                    self.audit.record("route_mismatch", "orchestrator", {"route": s.route, "error": str(exc)}, job_id)
                continue
            ws = self.workspaces.create(job_id=sub_id, kind=s.workspace, route=s.route)
            self._save(sub_id, job_id, s.title, State.RUNNING, s, workspace=ws)
            if self.audit:
                self.audit.record("subagent_started", "orchestrator",
                                  {"agent": s.agent, "route": s.route, "country": check.country, "workspace": ws}, job_id)
        return job_id

    def set_state(self, job_id: str, state: State, result: str | None = None) -> None:
        self.conn.execute("UPDATE jobs SET state = ?, result = COALESCE(?, result), updated_at = datetime('now') WHERE id = ?",
                          (state.value, result, job_id))

    def kill(self, job_id: str) -> None:
        """Kill destroys every workspace of the job and keeps the log (S1.6.3)."""
        for row in self.conn.execute("SELECT id, workspace FROM jobs WHERE id = ? OR parent_id = ?", (job_id, job_id)).fetchall():
            if row["workspace"]:
                self.workspaces.destroy(row["workspace"])
            self.set_state(row["id"], State.KILLED)
        if self.audit:
            self.audit.record("job_killed", "human", {"job": job_id}, job_id)

    def board(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200")]

    def _save(self, job_id, parent, title, state, s: Subtask | None = None, workspace=None, result=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO jobs(id, parent_id, title, state, agent, route, workspace, model, result) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, parent, title, state.value, s.agent if s else "orchestrator", s.route if s else None,
             workspace, s.model if s else OPUS, result),
        )
