# home-agents

The agent side of the system (Phase 1, roadmap **E1.1 to E1.11**): one agent service on the Claude Agent SDK that voice, the app and Telegram all talk to, an orchestrator that starts subagents in disposable workspaces, each pinned to an egress route, a message bus with a per-pair allow-list, and a credential broker that only relays ciphertext released from your phone.

**Status: skeleton.** Phase 0 comes first. What exists now is the code that must never be wrong and that agents may never change (the protected paths of S1.9.1):

| Module | What it enforces | Roadmap |
|---|---|---|
| `src/home_agents/policy.py` | Agent-to-agent messages only between allowed pairs; everything else blocked and logged | S1.5.2, S1.5.3 |
| `src/home_agents/guardrails.py` | Purchases: per-purchase and monthly caps in CAD, blocked categories (gift cards, crypto, transfers, security settings), approval always required | S1.8.3 |
| `src/home_agents/egress.py` | A job refuses to start unless its public IP and country match its route | S1.3.5 |
| `config/policy.example.yaml` | Example allow-list, caps and routes | |

Planned layout once Phase 1 starts: `service/` (agent service), `orchestrator/`, `broker/` (credential relay, ciphertext only), `workspaces/` (images and manager for the cloud VM), `bus/` (NATS config), `deploy/` (PR pipeline with rollback).

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]' && pytest
```
