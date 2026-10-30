# home-agents

The agent side of the system (roadmap **Phase 1** E1.1 to E1.11, **Phase 2** E2.2 to E2.7, **Phase 3** E3.5): one Claude agent behind voice, the app and Telegram; an orchestrator that runs subagents in disposable workspaces, each pinned to an egress route; a policy-checked message bus; approvals signed on your phone; and a credential broker that only ever relays ciphertext.

## Modules

| Module | What it does | Roadmap |
|---|---|---|
| `agent.py` | The single decision maker: tool loop, approval gates in code (an approved action runs as soon as the phone signs and is confirmed in the room that asked), token budget, refusal fallback, local fallback | E1.1, S2.3.2 |
| `models.py` | Haiku for routing, Sonnet by default, Opus for planning; spend in CAD with a monthly hard cap; private requests to the local model | S1.1.2, S1.8.2, S2.3.2 |
| `memory.py`, `db.py` | Household facts ("what do you know about me", "forget that") and shared conversation history | S1.1.3 |
| `tools.py` | Home Assistant tools through a scoped user; locks and garage doors need approval | S1.2.1, S0.5.9 |
| `approvals.py` | Requests signed by a P-256 key in the phone's Secure Enclave; nonce, expiry, single use | S1.6.2 |
| `broker.py` | Login release: phone seals the login to the workspace's one-time key; broker relays ciphertext once | E1.7 |
| `bus.py`, `policy.py` | Agent-to-agent messages only between allowed pairs; blocks are logged | S1.5.2, S1.5.3 |
| `orchestrator.py` | Plans subtasks (Opus, structured output), checks each route, starts workspaces, kill, spin-off budget | S1.5.1, S1.6.3, S2.5.1 |
| `workspaces.py` | Docker workspaces on an internal network behind the egress gateway; Tart macOS VMs (max 2) | S1.4.3, S1.3.4, S2.5.2 |
| `egress.py` | A job refuses to start unless the route's exit country (and IP) match | S1.3.5 |
| `guardrails.py`, `gmail_guard.py` | Purchase caps in CAD, gift cards/crypto/transfers blocked; agents never see security emails | S1.8.3 |
| `audit.py` | Searchable audit log with secret redaction, 30-day retention | S1.8.1 |
| `delivery.py` | One delivery rule for unprompted speech | S1.11.1 |
| `jobs/` | TTC reimbursement, package tracking, spreadsheet copies | E1.10 |
| `localllm.py` | Local model on the Studio | S2.3 |
| `routines.py` | Suggests automations from repeated manual actions | S2.7.1 |
| `scheduler.py` | Recurring jobs | E3.5 |
| `api.py` | HTTP API for HA, the app and Telegram | S1.1.1 |
| `harness/` | Local agent harness: any model (Claude, OpenAI, Gemini, OpenRouter, Ollama, MLX, llama.cpp), sessions, projects, background tasks, skills, MCP, heartbeat, Telegram, web console (`docs/harness.md`) | E1.12 |
| `ha_integration/custom_components/home_agent` | HA conversation agent that forwards Assist to this service | S1.1.1 |

## Deploy

There are two setups. In both, the vault, the credential broker and the egress routes (home-infra) stay on the Mac mini. What changes is where the agent runs.

| | **Mac mini only** (Phase 1) | **Mac mini + Mac Studio** (Phase 2 on) |
|---|---|---|
| Agent service + NATS | Hub Mac: `deploy/hub/compose.yaml`, localhost only | Studio: `deploy/studio/compose.yaml`, same image and the same `deploy/hub/` policy, keys and NATS config |
| Workspaces | Rented Linux VM on the tailnet: `deploy/workspace-host/` (egress gateway, Langfuse, images in `workspaces/`). Linux only, no macOS VMs | Studio: Docker workspaces and up to 2 Tart macOS VMs; the rented VM is cancelled after a week of both running |
| Models | Cloud only. Private requests fail instead of falling back to the cloud, because there is no local model | Cloud plus the local MLX model (`deploy/studio/launchd/`); private requests stay local |
| Voice | HA Assist on the hub Mac | Voice server on the Studio (LiveKit, Whisper, Piper); HA Assist on the hub Mac is the fallback |
| Web search for the harness | Claude's built-in search only | SearXNG in `deploy/studio/compose.yaml` for any model |
| Deploy | `deploy/deploy.sh <tag>` on the hub Mac | `deploy/deploy.sh <tag>` on the Studio |

Install, Mac mini only:

1. home-infra first (hub Mac baseline, vault, `services/egress/`).
2. On the hub Mac, in `deploy/hub/`: copy `.env.example` to `.env`, copy `config/policy.example.yaml` to `policy.yaml`, and put the phone's public key in `keys/`. Then run `deploy/deploy.sh <tag>` and `tailscale serve` port 8700.
3. Rent the workspace VM (16 to 32 GB, Ubuntu 24.04). In `deploy/workspace-host/` copy `.env.example` to `.env`, set `HUB_TAILNET_HOST`, then run `sudo ./setup.sh`. It joins the tailnet and blocks all inbound traffic.

Install, Mac mini + Mac Studio:

1. Everything above, so the agents work before the Studio arrives.
2. home-infra `docs/studio-checklist.md` on the Studio.
3. On the Studio: `deploy/studio/.env` (same keys as the hub Mac's, plus the Studio ones), the MLX launchd job, then `docker compose -f deploy/studio/compose.yaml up -d`.
4. Run both for a week, then stop the agent on the hub Mac and cancel the rented VM (`docs/phase-2.md`).

Agent code changes only by pull request (`docs/code-changes.md`); protected paths are listed in `PROTECTED_PATHS` and enforced by CI and CODEOWNERS. What's ready versus what needs the Studio is in `docs/phase-2.md`.

## Develop

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]' && pytest
```

Tests use fakes for Claude, Docker and the phone, so they run without keys or hardware.
