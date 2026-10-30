# Agent harness (E1.12)

The harness is the household's one assistant. The goal is a reliable home AI that can act like Jarvis, that you can watch and step into at any point, and that also works as the home's voice endpoint the way Alexa does.

It is a local, any-model version of these products:

- Meta Muse, Claude (app, Cowork and Dispatch), ChatGPT and Dot
- OpenClaw (clawdbot)
- Codex, Claude Code and Kiro
- CrewAI
- Alexa Routines and iPhone Shortcuts

It runs wherever the agent service runs, behind the same approvals, audit, spend cap and egress rules as everything else in this repo. The full feature comparison lives in the design docs ("Agent harness: feature comparison and plan"), and the routines plan is in `docs/routines.md`.

## Two setups

| | **Mac mini only** | **Mac mini + Mac Studio** |
|---|---|---|
| Where the harness runs | The agent service on the hub Mac (`deploy/hub/compose.yaml`) | The agent service on the Studio (`deploy/studio/compose.yaml`) |
| Chat, task and voice models | Cloud chains only (Claude, OpenAI, Gemini, OpenRouter). Private chats and tasks refuse instead of going to the cloud | Cloud plus local MLX, Ollama or llama.cpp on the Studio. `private` stays on the Studio |
| Long-term memory store | `memory-db` (Postgres + pgvector) on the mini, capped at 256 MB | `memory-db` on the Studio. Move it with `pg_dump` / `pg_restore` when you switch |
| Embeddings | A small local model (`nomic-embed-text` in Ollama on the mini, about 0.3 GB). With a cloud embedder instead, private memories get no vector and are found by full text only | Local embedding model on the Studio |
| Nightly memory sleep | 03:30 on the mini. With `allow_cloud: true`, non-private episodes are distilled by the `fast` cloud chain. Private ones wait, untouched, until a local model exists | 03:30 on the Studio, fully local |
| Voice | HA Assist on the mini calls the harness (`/v1/converse`). Cloud models answer | HA Assist on the mini calls the harness on the Studio. Local speech (Whisper, Piper) and local models; the mini's own Assist is the fallback |
| Routines | Engine in the agent service on the mini. Device-only routines stay HA automations | Engine on the Studio. HA on the mini still sends device and presence events, and its own automations keep running if the Studio is off |
| Coding tasks | Docker workspaces on the rented workspace VM | Docker workspaces on the Studio |
| Web search for any model | Claude's built-in search only (no SearXNG on the mini) | SearXNG in `deploy/studio/compose.yaml` |

## What is where

| Module | What it does |
|---|---|
| `harness/providers.py` | One message format for every model. `anthropic` for Claude; `openai` for OpenAI, Gemini's OpenAI endpoint, OpenRouter, and local Ollama, mlx_lm.server, llama.cpp, LM Studio and vLLM |
| `harness/router.py` | Model chains by alias (`fast`, `default`, `planning`, `private`, `advisor`), failover on outages, private mode (local only), and CAD spend per call. Cloud stops at the cap while local keeps working |
| `harness/loop.py` | Provider-neutral tool loop: approvals in code, token budget, steering, pause and cancel, and auto-compaction of long runs. Built-in tools: `second_opinion` (a different model reviews a plan) and `delegate_task` (subagents, two levels deep). A chat approval runs the tool once you approve it on the phone and reports back |
| `harness/profiles.py` | Agent profiles: persona, model chain, skills, and allow / ask / block per tool (stricter only). Built-in `assistant` and `voice` |
| `harness/store.py` | Projects, sessions (search, fork), tasks with an event trail, questions to you, feedback, and artifacts, in the shared SQLite file |
| `harness/tasks.py` | Background workers. Modes: `auto`, `plan` (read-only, then your OK), `spec` (requirements, design, tasks, each with your OK), `crew`, `flow`. Also covers retries with backoff, recovery after a restart, pause and resume, emergency stop, per-task model override, and the `ask_owner`, `update_todos`, `notify_owner` and `save_artifact` tools |
| `harness/coding.py` | Coding workspaces: a sandbox container and branch `agent/<task>` per task. Read, write, edit, grep, run, diff, checkpoints and rewind. It reads `AGENTS.md`, `CLAUDE.md`, `.claude/rules` and `.kiro/steering`. Opening a pull request always needs approval |
| `harness/crews.py` | CrewAI-style crews (sequential, or hierarchical with a manager that delegates) and flows (steps, routes, conditions), with retries against guardrails and an owner review step |
| `harness/outputs.py` | Expected output, JSON-schema structured output and guardrails (word limits, must / must-not include, regex) |
| `harness/hooks.py` | Event hooks: `tool_before` can block or ask, `tool_after`, `task_done` / `task_failed` can notify or start a task, plus `memory_sleep`. Rules never trigger themselves |
| `harness/recall.py` | Long-term memory: episodes, facts, summaries and knowledge. Hybrid search (pgvector cosine plus full text, fused by rank, weighted by strength). Private text never goes to a cloud embedder. Forgetting archives |
| `harness/sleep.py` | The nightly "sleep" job. It backfills embeddings, merges duplicates, distills episodes into a summary and facts, re-ranks by importance, age and use, and archives weak distilled episodes. Nothing is deleted, and every step is logged |
| `harness/routines.py` | Routines: triggers, conditions and steps (`docs/routines.md`). Device steps use the agent's tool path, so approvals and hooks apply. The agent's drafts stay off until you approve them on the phone |
| `harness/skills.py` | `skills/<name>/SKILL.md` (Agent Skills and OpenClaw format) and flat `skills/*.md`, loaded on demand with `load_skill` |
| `harness/mcp_tools.py` | MCP servers as tools `<server>__<tool>`. Anything that isn't read-only asks first |
| `harness/search.py` | `web_search` and `web_fetch` through self-hosted SearXNG, for models without built-in search |
| `harness/heartbeat.py` | Every 30 minutes a local model reads `HEARTBEAT.md` and the state. Silent unless something needs you |
| `harness/channels.py` | Telegram by long polling (no open port), pairing codes for unknown senders, a session per sender |
| `harness/api.py` | `/v1/h/*` routes covering chat, tasks, the live event stream (SSE), questions, pause, halt, feedback, memory, routines and metrics. Also signed webhooks at `/v1/hooks/<name>` and the console at `/console` |
| `harness/cli.py` | `home-agent` command: chat, task (any mode, `--repo`, `--model`), watch, steer, pause, resume, stop, answer, halt, recall, sleep |
| `harness/daemon.py` | Heartbeat, channels, the routine clock and HA state feed, and the nightly memory sleep. Phone notifications go through HA's notify service |
| `harness/console/index.html` | Web console: chat, tasks with live steps, projects, memory, approvals (read-only), models and skills, pairing |

## How it fits with Home Assistant

- **HA into the harness.** HA's Assist pipeline, the Telegram and briefing automations, and the app call the `home_agent` conversation integration. That integration posts to `/v1/converse`. With `voice_through_harness: true` (the default), that goes through the harness with the `voice` profile, so voice gets the same models, memory, hooks and routines as everything else. A routine's phrase ("good night") runs the routine directly, with no model call. Simple device commands never leave HA, because HA's local intents handle them first.
- **The harness into HA.** The harness controls HA through `ha_call_service` and `ha_states`, over HA's REST API. The token belongs to a non-admin HA user that sees only exposed entities. Locks, the alarm, buttons and the garage always need the phone's approval.
- **Events.** For routine triggers, the daemon polls HA states (`routines.poll_seconds`). An HA automation can also push events to `/v1/h/routines/events` for instant ones. Phone sensors (alarm, charger, focus, Wi-Fi, NFC tags) arrive through the HA companion app.

## Run it

1. Copy the config files:
   - `cp config/harness.example.yaml config/harness.yaml` and `cp config/HEARTBEAT.example.md config/HEARTBEAT.md`. Replace the `<...>` model names, and set `routines.location` on the machine only.
   - Both compose files mount `config/harness.yaml`, so create it before you start them.
2. Put only the keys you use in the ignored `.env`:
   - The providers' keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` and `OPENROUTER_API_KEY`. Local models need none.
   - Also `TELEGRAM_BOT_TOKEN` and any hook secrets.
   - `MEMORY_DB_PASSWORD` and `HOME_AGENTS_PG_DSN` for the memory database.
3. Start it for your setup:
   - **Mac mini only:** run `deploy/deploy.sh <tag>` on the hub Mac. That starts `memory-db` too.
     - Use cloud providers only. The 8 GB Mac mini already runs the HA VM, so the only local model is the small embedding model (`ollama pull nomic-embed-text`).
     - `private` chats fail until there is a local model.
     - `web_search` needs Claude's built-in search, because SearXNG isn't deployed here.
     - Set `memory.sleep.allow_cloud: true` if you want non-private memories distilled by a cloud model.
   - **Mac mini + Mac Studio:** on the Studio, start `ollama serve` or the MLX launchd job, then run `docker compose -f deploy/studio/compose.yaml up -d`. That includes SearXNG and `memory-db`.
     - To move memory from the mini, run `pg_dump` on the mini's `memory-db` and `pg_restore` it into the Studio's.
     - Point HA's `home_agent` integration at the Studio.
4. Run `tailscale serve` on the agent port as before, then open `https://<agent-host>.<tailnet>.ts.net/console` and paste the service token. For the CLI, set `HOME_AGENTS_URL` and `HOME_AGENTS_TOKEN` on your laptop.

With no `config/harness.yaml`, the service runs exactly as before.

## Rules it keeps

- Nothing listens outside localhost. The console, webhooks and the memory database are reachable only on the tailnet or the compose network, and Telegram is polled outward.
- Approvals are decided only on the phone with its signature; the console shows them read-only. This also covers routines the agent drafts, routine confirm steps, and coding pull requests.
- Private chats, tasks and memories use local providers only. If none is up, they fail or wait instead of falling back to the cloud.
- Memory is never hard-deleted by the agent. Merges and pruning archive, with a reason in `memory_events`.
- Hooks and routines can't loop. Work they start never re-triggers them, nested routines stop at three levels, and a run stops after 200 steps.
- Skills from OpenClaw's registry or anywhere else are read, reviewed and added by pull request. Nothing installs at runtime.
- Artifacts are agent output. The console downloads them and never renders them on its own origin.
- `loop.py`, `profiles.py` and `router.py` are protected paths: an agent's pull request can't change them.

## Tests

`pytest -q` runs everything on SQLite. To include the Postgres + pgvector memory tests, set `HOME_AGENTS_TEST_PG` to a scratch database, for example a local `pgvector/pgvector` container. CI does this.

## Not done yet

- More channels (Signal, WhatsApp, Slack) behind the same `Channel` interface. Pairings persist only in `channels.allowed` for now.
- An MCP server for Home Assistant and a WebSocket event feed. Polling and pushed events cover routines for now.
- Live artifact previews in a sandboxed frame, image generation, and one-time virtual cards for purchases.
- Console pages for routines, questions and memory. The routes exist, and the phone app (home-app, E0.8) uses the same `/v1/h/*` routes.
