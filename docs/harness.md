# Agent harness (E1.12)

The harness turns the agent service into a local, any-model version of Meta Muse, Claude Cowork and Dispatch, OpenClaw and ChatGPT: chat sessions and projects, background tasks you can steer or stop, skills, MCP connectors, a heartbeat, chat-app channels and a web console. It runs wherever the agent service runs (the hub Mac with the Mac mini only, the Studio with the Mac mini + Mac Studio) behind the same approvals, audit, spend cap and egress rules as everything else here. The full feature comparison lives in the design docs ("Agent harness: feature comparison and plan").

## What is where

| Module | What it does |
|---|---|
| `harness/providers.py` | One message format for every model. `anthropic` for Claude; `openai` for OpenAI, Gemini's OpenAI endpoint, OpenRouter, and local Ollama, mlx_lm.server, llama.cpp, LM Studio, vLLM |
| `harness/router.py` | Model chains by alias (`fast`, `default`, `planning`, `private`), failover on outages, private mode (local only), CAD spend per call, cloud stops at the cap while local keeps working |
| `harness/loop.py` | Provider-neutral tool loop: approvals in code, token budget, steering, cancel, waits for the phone in tasks |
| `harness/profiles.py` | Agent profiles: persona, model chain, skills, allow / ask / block per tool (stricter only) |
| `harness/store.py` | Projects, sessions, tasks with an event trail, artifacts, in the shared SQLite file |
| `harness/tasks.py` | Background task workers; attachments (images to the model, text inlined); `save_artifact` tool; notify when done |
| `harness/skills.py` | `skills/<name>/SKILL.md` (Agent Skills and OpenClaw format) and flat `skills/*.md`; `load_skill` on demand |
| `harness/mcp_tools.py` | MCP servers as tools `<server>__<tool>`; anything not read-only asks first |
| `harness/search.py` | `web_search` and `web_fetch` through self-hosted SearXNG, for models without built-in search |
| `harness/heartbeat.py` | Every 30 minutes a local model reads `HEARTBEAT.md` and the state; silent unless something needs you |
| `harness/channels.py` | Telegram by long polling (no open port), pairing codes for unknown senders, a session per sender |
| `harness/api.py` | `/v1/h/*` routes, signed webhooks at `/v1/hooks/<name>`, the console at `/console` |
| `harness/daemon.py` | Starts the heartbeat and channel loops; phone notifications through HA's notify service |
| `harness/console/index.html` | Web console: chat, tasks with live steps, projects, memory, approvals (read-only), models and skills, pairing |

## Run it

1. `cp config/harness.example.yaml config/harness.yaml` and `cp config/HEARTBEAT.example.md config/HEARTBEAT.md`; replace the `<...>` model names with ones you use.
2. Put only the keys you use in the ignored `.env` (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, hook secrets). Local models need none.
3. Start it for your setup:
   - **Mac mini only:** `deploy/deploy.sh <tag>` on the hub Mac. Use cloud providers only. The 8 GB Mac mini already runs the HA VM, so don't run a local model on it. `private` chats fail until there is a local model, and `web_search` needs Claude's built-in search because SearXNG isn't deployed.
   - **Mac mini + Mac Studio:** on the Studio, `ollama serve` or the MLX launchd job, then `docker compose -f deploy/studio/compose.yaml up -d`. That includes SearXNG.
4. `tailscale serve` the agent port as before, open `https://<agent-host>.<tailnet>.ts.net/console` and paste the service token.

With no `config/harness.yaml` the service runs exactly as before.

## Rules it keeps

- Nothing listens outside localhost; the console and webhooks are reachable only on the tailnet, and Telegram is polled outward.
- Approvals are decided only on the phone with its signature; the console shows them read-only.
- Private chats and tasks use local providers only. If none is up, they fail instead of falling back to the cloud.
- Skills from OpenClaw's registry or anywhere else are read, reviewed and added by pull request. Nothing installs at runtime.
- Artifacts are agent output: the console downloads them, never renders them on its own origin.
- `loop.py`, `profiles.py` and `router.py` are protected paths: an agent's pull request can't change them.

## Not done yet

- Voice still uses `agent.py` (Claude-specific: server-side web search, prompt caching, fallbacks). It moves onto the router once a local model is benchmarked (S2.3.1).
- More channels (Signal, WhatsApp, Slack) behind the same `Channel` interface; pairings persist only in `channels.allowed` for now.
- Live artifact previews in a sandboxed frame, image generation, and one-time virtual cards for purchases.
- The phone app shows tasks and artifacts through the same `/v1/h/*` routes (home-app, E0.8).
