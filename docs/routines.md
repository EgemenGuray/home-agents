# Routines (E1.12)

Routines work like Alexa Routines and iPhone Shortcuts automations. Each one has triggers ("when"), optional conditions ("only if") and steps ("do"). The engine is `src/home_agents/harness/routines.py`. A step can be plain device control or an AI step, such as "summarize my day and say it in the kitchen".

## How it compares

| | Alexa Routines | iPhone Shortcuts | Google Home | HA automations | **Ours** |
|---|---|---|---|---|---|
| Voice phrase | ✓ | ✓ (Siri) | ✓ | ✓ | ✓ matched before any model runs |
| Time, days, date, interval | ✓ | ✓ | ✓ | ✓ | ✓ |
| Sunrise / sunset with offset | ✓ | ✓ | ✓ | ✓ | ✓ computed locally |
| Device state, thresholds, "for N minutes" | ✓ | some | ✓ | ✓ | ✓ |
| Arrive / leave a zone | ✓ | ✓ | ✓ | ✓ | ✓ |
| Phone: alarm, charger, focus, Wi-Fi, NFC | alarm | ✓ | — | via the companion app | ✓ via the companion app |
| Button, widget, URL | Echo Button | ✓ | ✓ | ✓ | ✓ app, console, CLI, a Shortcut calling the URL |
| Webhook / event from another service | — | — | — | ✓ | ✓ |
| Conditions, if / otherwise, variables | time window | ✓ | ✓ | ✓ | ✓ |
| Ask before running | — | ✓ | — | — | ✓ signed on the phone |
| "Type what you'd say" | ✓ | — | ✓ | — | ✓ `ask_agent`, with the answer in a variable |
| Start a background agent task | — | — | — | — | ✓ |
| Cooldown, run log, test run | history | ✓ | history | ✓ | ✓ |
| Locks and the alarm need approval | PIN | — | PIN | — | ✓ phone signature |

## Writing one

```yaml
name: Morning
triggers:
  - {type: phrase, phrases: ["good morning"]}
  - {type: time, at: "06:45", days: weekdays}
conditions:
  - {type: someone_home}
steps:
  - {device: {domain: light, service: turn_on, area_id: kitchen, data: {brightness_pct: 60}}}
  - {ask_agent: "Today's weather and my first meeting, in two sentences", save_as: brief}
  - {say: "{brief}", room: kitchen}
cooldown_minutes: 60
```

**Triggers:**

- `phrase` with `phrases`
- `time` with `at` (HH:MM), plus optional `days` (`daily`, `weekdays`, `weekends` or a list) and `date`
- `interval` with `minutes`
- `sun` with `event` (`sunrise` or `sunset`) and an optional `offset_minutes`
- `state` with `entity` and any of `to`, `from`, `above`, `below` and `for_minutes`
- `presence` with `person`, `event` (`arrive` or `leave`) and an optional `zone`
- `event` with `event_type` and optional `data`
- `tag` with `tag_id`
- `webhook` with `name`
- `manual`
- `harness` with `event` (`task_done`, `task_failed` or `memory_sleep`)

**Conditions:**

- `time` with `after` and `before`. The window can cross midnight.
- `days`
- `state` with `entity` and any of `is`, `is_not`, `above` and `below`
- `someone_home`
- `sun` with `is` (`up` or `down`)
- `var` with `name` and `equals` or `contains`

Any condition can take `not: true`.

**Steps:**

- `device`: an HA service call, which goes through approvals and hooks
- `say` with an optional `room`
- `notify`
- `wait` (seconds)
- `ask_agent` with `save_as`
- `task` with `mode`
- `confirm`: a yes/no on the phone
- `if` / `then` / `else`
- `set`
- `run_routine`
- `stop`

Templates like `{brief}` use the routine's variables, plus scalar fields from the trigger (`entity`, `to`, `phrase`, and a task's `title`).

## Where routines come from

- **`config/harness.yaml` → `routines.defaults`:** added once on start.
- **The API:** `POST /v1/h/routines` (console, app, CLI). These are on as soon as they're saved.
- **The agent:** `create_routine` from "every weekday at 7, turn on the kitchen lights". These are saved switched off until you approve them on your phone.
- **Habit suggestions:** `src/home_agents/routines.py` (S2.7.1) turns habits it spots into drafts.

## Running them

- **Clock:** the daemon ticks every 30 seconds, for time, interval, sun and "for N minutes" triggers.
- **HA states:** the daemon polls every `routines.poll_seconds`. For instant triggers, an HA automation can push to `POST /v1/h/routines/events` with `{entity_id, new_state, old_state}` or `{event_type, data}`.
- **Buttons and Shortcuts:** `POST /v1/h/routines/<id or name>/run` over Tailscale with the service token. `?dry_run=true` walks the steps without doing anything.
- **Webhooks:** `POST /v1/h/routines/webhook/<name>`.
- **Run log:** each run's steps and variables are in `GET /v1/h/routines/<id>/runs`.

## Mac mini only, and Mac mini + Mac Studio

- **Mac mini only:** the engine runs in the agent service on the hub Mac, and `ask_agent` uses cloud models. Keep device-only routines, like lights at sunset or good night, as Home Assistant automations (home-assistant-config `packages/`), because they keep working when the agent service is down.
- **Mac mini + Mac Studio:** the engine runs on the Studio with local models. HA on the mini still feeds it states and events. If the Studio is off, HA's own automations keep the house running, and harness routines resume when it's back. Missed time triggers aren't replayed.
