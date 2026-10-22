"""The local agent harness (E1.12): any model, chat sessions, projects, background tasks, skills,
MCP connectors, heartbeat, chat channels and a web console, on top of the safety code in this
package. See docs/harness.md for the feature map against Muse, Cowork, Dispatch, OpenClaw and ChatGPT.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..approvals import Approvals
from ..audit import AuditLog
from ..memory import Memory
from ..models import Spend
from ..tools import Tool, ToolRegistry
from .loop import HarnessAgent, Hooks, RunResult
from .profiles import Profile, load_profiles
from .providers import build_providers
from .router import ModelRouter
from .search import SearxSearch
from .skills import SkillLibrary
from .store import Store
from .tasks import TaskRunner


@dataclass
class Harness:
    store: Store
    router: ModelRouter
    agent: HarnessAgent
    profiles: dict[str, Profile]
    tasks: TaskRunner
    skills: SkillLibrary
    memory: Memory

    def chat(self, session_id: str | None, text: str, *, profile: str | None = None, project_id: str | None = None,
             private: bool | None = None, channel: str = "console") -> tuple[dict, RunResult]:
        session = self.store.session(session_id) if session_id else None
        if session is None:
            project = self.store.project(project_id) or {}
            session = self.store.create_session(profile or project.get("profile") or "assistant", project_id)
        prof = self.profiles.get(profile or session["profile"]) or self.profiles["assistant"]
        project = self.store.project(session["project_id"]) or {}
        history = [{"role": m["role"], "content": m["content"]} for m in self.store.messages(session["id"])]
        self.store.add_message(session["id"], "user", text, channel=channel)
        r = self.agent.run(history + [{"role": "user", "content": text}], prof, project_instructions=project.get("instructions", ""),
                           channel=channel, private=private, hooks=Hooks())
        self.store.add_message(session["id"], "assistant", r.text, model=r.models[-1] if r.models else None, channel=channel)
        return self.store.session(session["id"]), r


def build_harness(config: dict, *, conn, data_dir: Path, audit: AuditLog, memory: Memory, spend: Spend,
                  approvals: Approvals | None, base_tools: ToolRegistry, anthropic_client=None, providers=None,
                  extra_tools: list[Tool] = (), start_workers: bool = True) -> Harness:
    providers = providers if providers is not None else build_providers(config.get("providers", {}), anthropic_client)
    router = ModelRouter(providers, config.get("models", {}), spend, audit, config.get("prices_usd"))
    tools = ToolRegistry(list(base_tools._tools.values()) + list(extra_tools))
    search_url = (config.get("search") or {}).get("searxng_url")
    if search_url:
        for t in SearxSearch(search_url).tools():
            tools.add(t)
    skills = SkillLibrary(config.get("skills_dir", "skills"))
    agent = HarnessAgent(router, tools, audit, approvals, skills, facts=memory.facts_prompt)
    profiles = load_profiles(config.get("profiles", {}))
    store = Store(conn, data_dir)
    runner = TaskRunner(store, agent, profiles, workers=int(config.get("task_workers", 2)), start=start_workers)
    return Harness(store, router, agent, profiles, runner, skills, memory)
