# Phase 3 agent extras (E3.5)

- Recurring jobs: `scheduler.py` (monthly TTC report, weekly price watch). Each run goes through the orchestrator and approvals like a spoken request.
- Skills library: `skills/`, one Markdown file per reusable procedure that subagents load by name.
- Vision ("what am I holding?", "who came to the door?") uses the local model on the Studio so camera footage stays home; it needs a camera choice first.
- Per-person voices (speaker recognition) keep reminders and calendars separate; it needs the voice server running first.
