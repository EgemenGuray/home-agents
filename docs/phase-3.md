# Phase 3 agent extras (E3.5)

- Recurring jobs: `scheduler.py` (monthly TTC report, weekly price watch). Each run goes through the orchestrator and approvals like a spoken request. Works in both setups.
- Skills library: `skills/`, one Markdown file per reusable procedure that subagents load by name. Works in both setups.
- Vision ("what am I holding?", "who came to the door?") uses the local model on the Studio so camera footage stays home; it needs a camera choice first. **Mac mini + Mac Studio only**: with the Mac mini only it is not offered, because the footage would have to go to the cloud.
- Per-person voices (speaker recognition) keep reminders and calendars separate; it needs the voice server running first, so **Mac mini + Mac Studio only**.
