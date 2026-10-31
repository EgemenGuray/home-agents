"""The local agent harness (E1.12): any model, chat sessions, projects, background tasks, coding
workspaces, plan and spec modes, crews and flows, skills, MCP connectors, hooks, long-term memory
with nightly consolidation, heartbeat, chat channels, a CLI and a web console, on top of the safety
code in this package. docs/harness.md has the feature map and what runs where on the Mac mini alone
and on the Mac mini plus Mac Studio.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ..approvals import Approvals
from ..audit import AuditLog
from ..memory import Memory
from ..models import Spend
from ..tools import Tool, ToolRegistry
from .crews import CrewRunner
from .hooks import EventHooks
from .loop import HarnessAgent, Hooks, RunResult
from .profiles import Profile, load_profiles
from .providers import build_providers
from .recall import Recall, build_recall
from .router import ModelRouter
from .routines import RoutineEngine, tool_runner
from .search import SearxSearch
from .skills import SkillLibrary
from .store import Store
from .tasks import TaskRunner

MEMORY_TOOLS = ("remember_fact", "forget_fact", "recall_memory", "save_memory")


@dataclass
class Harness:
    store: Store
    router: ModelRouter
    agent: HarnessAgent
    profiles: dict[str, Profile]
    tasks: TaskRunner
    skills: SkillLibrary
    memory: Memory
    recall: Recall | None = None
    events: EventHooks | None = None
    crews: CrewRunner | None = None
    converse_enabled: bool = True
    routines: RoutineEngine | None = None

    def converse(self, text: str, conversation_id: str | None, *, channel: str = "voice", room: str | None = None) -> tuple[str, RunResult]:
        """Home Assistant's conversation agent and the app's Assistant tab (`/v1/converse`) come through here,
        so voice gets the same models, memory, hooks and approvals as everything else."""
        key = f"{channel}:{conversation_id}" if conversation_id else None
        profile = "voice" if channel == "voice" and "voice" in self.profiles else "assistant"
        session = self.store.session_for(key, profile) if key else None
        where = f"{channel} in the {room}" if room else channel
        session, r = self.chat(session["id"] if session else None, text, profile=profile, channel=where)
        return conversation_id or session["id"], r

    def chat(self, session_id: str | None, text: str, *, profile: str | None = None, project_id: str | None = None,
             private: bool | None = None, channel: str = "console", model: str | None = None,
             temporary: bool = False) -> tuple[dict | None, RunResult]:
        """One chat turn. `temporary` is the Claude app's temporary chat: nothing is stored or remembered.
        A routine's phrase ("good night") runs that routine straight away, without a model."""
        if self.routines is not None and (hit := self.routines.match_phrase(text)):
            return self._run_phrase(session_id, text, hit, channel, temporary)
        prof_name = profile
        session = None
        history: list[dict] = []
        if not temporary:
            session = self.store.session(session_id) if session_id else None
            if session is None:
                project = self.store.project(project_id) or {}
                session = self.store.create_session(profile or project.get("profile") or "assistant", project_id)
            prof_name = profile or session["profile"]
            project_id = session["project_id"]
            history = [{"role": m["role"], "content": m["content"]} for m in self.store.messages(session["id"])]
        prof = self.profiles.get(prof_name or "assistant") or self.profiles["assistant"]
        if model:
            prof = replace(prof, model=model)
        extra: list[Tool] = []
        memory = ""
        if temporary:
            prof = replace(prof, block=tuple(prof.block) + MEMORY_TOOLS)
        elif self.recall is not None:
            memory = self.recall.prompt(text, project_id=project_id, include_private=True)
            extra = self.recall.tools(project_id)
        project = self.store.project(project_id) or {}
        if session:
            self.store.add_message(session["id"], "user", text, channel=channel)
        r = self.agent.run(history + [{"role": "user", "content": text}], prof, project_instructions=project.get("instructions", ""),
                           channel=channel, private=private, hooks=Hooks(), extra_tools=extra, memory=memory)
        if session:
            self.store.add_message(session["id"], "assistant", r.text, model=r.models[-1] if r.models else None, channel=channel)
            if self.recall is not None:
                is_private = bool(private if private is not None else prof.private)
                self.recall.add(f"[{channel}] Asked: {text[:600]} | Answered: {r.text[:600]}", "episode",
                                source=f"chat:{session['id']}", project_id=project_id, private=is_private)
            session = self.store.session(session["id"])
        return session, r


    def _run_phrase(self, session_id, text, hit, channel, temporary) -> tuple[dict | None, RunResult]:
        routine, phrase = hit
        run = self.routines.run(routine["id"], {"type": "phrase", "phrase": phrase, "channel": channel})
        if run.state == "skipped":
            reply = f"{routine['name']} doesn't apply right now."
        elif run.state == "failed":
            reply = f"{routine['name']} ran into a problem; the details are in the routine's log."
        else:
            reply = " ".join(run.spoken) or "OK."
        session = None
        if not temporary:
            session = (self.store.session(session_id) if session_id else None) or self.store.create_session("assistant")
            self.store.add_message(session["id"], "user", text, channel=channel)
            self.store.add_message(session["id"], "assistant", reply, model=f"routine:{routine['name']}", channel=channel)
            session = self.store.session(session["id"])
        return session, RunResult(reply, [], models=[f"routine:{routine['name']}"])


def build_harness(config: dict, *, conn, data_dir: Path, audit: AuditLog, memory: Memory, spend: Spend,
                  approvals: Approvals | None, base_tools: ToolRegistry, anthropic_client=None, providers=None,
                  extra_tools: list[Tool] = (), start_workers: bool = True, notify=lambda text: None,
                  code_workspaces=None) -> Harness:
    providers = providers if providers is not None else build_providers(config.get("providers", {}), anthropic_client)
    router = ModelRouter(providers, config.get("models", {}), spend, audit, config.get("prices_usd"))
    tools = ToolRegistry(list(base_tools._tools.values()) + list(extra_tools))
    search_url = (config.get("search") or {}).get("searxng_url")
    if search_url:
        for t in SearxSearch(search_url).tools():
            tools.add(t)
    skills = SkillLibrary(config.get("skills_dir", "skills"))
    store = Store(conn, data_dir)
    recall = build_recall(config.get("memory") or {}, conn, audit)
    profiles = load_profiles(config.get("profiles", {}))
    holder: dict = {}
    events = EventHooks(config.get("event_hooks") or [], notify=notify, audit=audit,
                        start_task=lambda prompt, profile, title: holder["runner"].submit(prompt, profile=profile, title=title,
                                                                                          source=f"rule:{title}"))
    agent = HarnessAgent(router, tools, audit, approvals, skills, facts=memory.facts_prompt, events=events,
                         lessons=store.lessons, followup=lambda text, channel: notify(text))
    agent.profiles = profiles
    crews = CrewRunner(agent, profiles, config.get("crews") or {}, config.get("flows") or {})
    runner = TaskRunner(store, agent, profiles, workers=int(config.get("task_workers", 2)), start=start_workers,
                        recall=recall, events=events, crews=crews, code_workspaces=code_workspaces)
    holder["runner"] = runner
    routines = None
    rcfg = config.get("routines") or {}
    if rcfg is not False and rcfg.get("enabled", True):
        rprof = profiles.get(rcfg.get("profile", "assistant")) or profiles["assistant"]
        routines = RoutineEngine(
            conn, run_tool=tool_runner(agent, rprof),
            ask_agent=lambda prompt, prof: agent.run([{"role": "user", "content": prompt}],
                                                     profiles.get(prof) or profiles["assistant"], channel="routine").text,
            start_task=runner.submit, notify=notify, approvals=approvals, audit=audit,
            location=rcfg.get("location"), say=rcfg.get("say"))
        for t in routines.tools():
            tools.add(t)
        for d in rcfg.get("defaults") or []:  # routines from harness.yaml, added once
            if routines.by_name(d["name"]) is None:
                routines.add(d)
        events.listeners.append(lambda event, ctx: routines.on_event(event, ctx, datetime.now().astimezone()))
    return Harness(store, router, agent, profiles, runner, skills, memory, recall, events, crews, routines=routines)
