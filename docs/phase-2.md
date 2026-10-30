# Phase 2 on the Mac Studio (E2.1 to E2.7)

This is the move from the **Mac mini only** setup to the **Mac mini + Mac Studio** setup. It starts the day the Studio arrives. Nothing here changes the Mac mini only setup, which keeps working as it is until you switch. The table lists what is ready in this repo and what still needs the hardware.

| Item | Ready here | Needs the Studio |
|---|---|---|
| E2.2 move the runtime | `deploy/studio/compose.yaml` runs the same image; the hub Mac keeps vault, broker and routes | Run both for a week, then cancel the cloud VM (S2.2.2) |
| E2.2.3 runtime changes by PR | `protected-paths`, `deploy/deploy.sh` rollback | A staging copy of the service on the Studio to replay recent requests against |
| E2.3 local models | `localllm.py`, `choose_model` sends private requests local and falls back when the cloud is down (tested) | Pick and benchmark the model (100B to 200B class, 4-bit MLX) with 20 workspaces running; `launchd/home.studio.llm.plist` |
| E2.4 voice server | LiveKit, Whisper and Piper services in compose; Assist on the hub Mac can use the Studio's Whisper/Piper over Wyoming right away | The LiveKit voice worker (wake word, barge-in, HA intents first, then the agent) and devices v2; written against the hardware so latency can be measured |
| E2.5 spin-off and GUIs | `SpinOffBudget`, `request_peer`, `TartWorkspaces` (max 2 macOS VMs), `WorkspaceRouter` | A Tart macOS base image with pf rules forcing its proxy, Excel installed, no Apple ID |
| E2.6 Macs watch each other | home-infra `services/monitoring/` (Uptime Kuma + Beszel on both Macs) | Restore rehearsal of the HA backup onto the Studio |
| E2.7 routine suggestions | `routines.py` finds repeated manual actions and writes the automation YAML (tested) | Feed it HA's logbook; suggestions land as pull requests to home-assistant-config |
