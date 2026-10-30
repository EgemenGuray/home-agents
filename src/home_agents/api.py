"""HTTP API of the agent service (S1.1.1). Bind to 127.0.0.1 and publish with `tailscale serve`.

Callers: the HA conversation agent (custom_components/home_agent), the app's Assistant and
Agents tabs, and the Telegram bridge. Client calls carry a service token; approvals are
authenticated by the phone's signature instead, so a stolen token can't approve anything.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .agent import Agent, BudgetExceeded
from .approvals import ApprovalError, Approvals
from .audit import AuditLog
from .broker import BrokerError, CredentialBroker
from .db import connect
from .memory import Memory
from .models import Spend, SpendExceeded
from .settings import Settings
from .tools import HomeAssistant, ToolRegistry, memory_tools


class ConverseIn(BaseModel):
    text: str
    conversation_id: str | None = None
    channel: str = "voice"
    room: str | None = None


class DecisionIn(BaseModel):
    decision: str
    signature: str
    ciphertext: str = ""


class FactIn(BaseModel):
    subject: str
    fact: str


def build_app(settings: Settings | None = None, client=None, approvals: Approvals | None = None,
              harness_config: dict | None = None, providers=None, start_workers: bool = True) -> FastAPI:
    settings = settings or Settings()
    conn = connect(settings.db_path)
    audit = AuditLog(conn)
    memory = Memory(conn)
    spend = Spend(conn, settings.usd_to_cad, settings.daily_alert_cad, settings.monthly_cap_cad)
    policy = yaml.safe_load(Path(settings.policy_file).read_text()) if Path(settings.policy_file).exists() else {}

    if approvals is None:
        keys = [Path(p).read_bytes() for p in policy.get("approvals", {}).get("phone_public_keys", [])]
        approvals = Approvals(keys, audit=audit) if keys else None
    broker = CredentialBroker(approvals, set(policy.get("agents_sites", [])), audit) if approvals else None

    tools = ToolRegistry(memory_tools(memory))
    ha = HomeAssistant(settings.ha_url, settings.ha_token) if settings.ha_url and settings.ha_token else None
    if ha:
        for t in ha.tools():
            tools.add(t)
    if client is None:  # pragma: no cover
        import anthropic
        client = anthropic.Anthropic()
    agent = Agent(client, tools, memory, spend, audit, approvals, settings.request_token_budget)

    service_token = os.environ.get("HOME_AGENTS_SERVICE_TOKEN", "")

    def auth(authorization: str = Header(default="")) -> None:
        if not service_token or not hmac.compare_digest(authorization, f"Bearer {service_token}"):
            raise HTTPException(401, "bad token")

    app = FastAPI(title="home-agents", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "month_cad": round(spend.month(), 2)}

    @app.post("/v1/converse", dependencies=[Depends(auth)])
    def converse(body: ConverseIn):
        try:
            r = agent.converse(body.text, body.conversation_id, channel=body.channel, room=body.room)
        except SpendExceeded as exc:
            return {"text": f"I've reached this month's spending cap, so I can only do simple commands. ({exc})",
                    "conversation_id": body.conversation_id}
        except BudgetExceeded:
            return {"text": "That got too long for one request. Try asking in smaller steps.",
                    "conversation_id": body.conversation_id}
        return {"text": r.text, "conversation_id": r.conversation_id, "pending_approvals": r.pending_approvals}

    @app.get("/v1/memory", dependencies=[Depends(auth)])
    def list_memory():
        return memory.facts()

    @app.post("/v1/memory", dependencies=[Depends(auth)])
    def add_memory(body: FactIn):
        return {"id": memory.remember(body.subject, body.fact)}

    @app.delete("/v1/memory/{fact_id}", dependencies=[Depends(auth)])
    def delete_memory(fact_id: int):
        return {"deleted": memory.forget(fact_id)}

    @app.get("/v1/approvals", dependencies=[Depends(auth)])
    def list_approvals():
        return [r.public_view() for r in approvals.pending()] if approvals else []

    @app.post("/v1/approvals/{request_id}")
    def decide(request_id: str, body: DecisionIn):
        if approvals is None:
            raise HTTPException(503, "approvals not configured")
        try:
            if broker and request_id in broker._requests:
                broker.phone_response(request_id, body.decision, body.signature, body.ciphertext)
                return {"state": approvals.get(request_id).state}
            return {"state": approvals.decide(request_id, body.decision, body.signature).state}
        except (ApprovalError, BrokerError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/v1/audit", dependencies=[Depends(auth)])
    def search_audit(q: str = "", kind: str | None = None):
        return audit.search(q, kind)

    # The harness (E1.12): any model, sessions, projects, tasks, skills, MCP, channels and the console.
    if harness_config is None and Path(settings.harness_file).exists():
        harness_config = yaml.safe_load(Path(settings.harness_file).read_text()) or {}
    if harness_config is not None:
        from .harness import build_harness
        from .harness.api import mount
        from .harness.channels import Pairing
        from .harness.mcp_tools import load_mcp_tools

        mcp = load_mcp_tools(harness_config.get("mcp", {})) if harness_config.get("mcp") else []
        harness = build_harness(harness_config, conn=conn, data_dir=settings.data_dir, audit=audit, memory=memory,
                                spend=spend, approvals=approvals, base_tools=tools, anthropic_client=client,
                                providers=providers, extra_tools=mcp, start_workers=start_workers)
        pairing = Pairing(set((harness_config.get("channels") or {}).get("allowed", [])))
        mount(app, harness, auth, harness_config.get("hooks"), pairing)
        if start_workers:
            from .harness import daemon
            notify = daemon.phone_notifier(ha, (harness_config.get("notify") or {}).get("service"))
            app.state.harness_stop = daemon.start(harness, harness_config, pairing, notify)
        app.state.harness = harness
        app.state.pairing = pairing

    @app.get("/metrics", dependencies=[Depends(auth)])
    def metrics():
        from fastapi.responses import PlainTextResponse

        from .metrics import render
        return PlainTextResponse(render(conn, spend, approvals, getattr(app.state, "harness", None)),
                                 media_type="text/plain; version=0.0.4")

    app.state.agent = agent
    app.state.approvals = approvals
    app.state.broker = broker
    return app
