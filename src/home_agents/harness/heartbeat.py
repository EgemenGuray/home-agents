"""Heartbeat (E1.12): OpenClaw's idea, done the quiet way.

Every so often a cheap model (local first) reads HEARTBEAT.md, the open tasks and the waiting
approvals, and either answers HEARTBEAT_OK or writes one short message. The message goes
through the one delivery rule (S1.11.1), so it never wakes anyone. The heartbeat only talks;
it can't start tasks or call tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Callable

from ..delivery import Urgency
from .router import ModelRouter, NoModelAvailable

OK = "HEARTBEAT_OK"
PROMPT = f"""You are the household agent's heartbeat. Read the checklist and the state below. If nothing \
needs the owner's attention now, reply with exactly {OK}. Otherwise reply with one or two short \
sentences for their phone. Start with "HEADS UP:" only if it matters within the hour."""


@dataclass
class Beat:
    message: str | None
    urgency: Urgency = Urgency.INFO


class Heartbeat:
    def __init__(self, router: ModelRouter, checklist: Path | str, state: Callable[[], str],
                 deliver: Callable[[Beat], None], model: str = "fast", private: bool = True,
                 active: tuple[time, time] = (time(7, 0), time(22, 30))):
        self.router = router
        self.checklist = Path(checklist)
        self.state = state
        self.deliver = deliver
        self.model = model
        self.private = private
        self.active = active

    def due(self, now: datetime) -> bool:
        start, end = self.active
        return start <= now.time() < end

    def beat(self, now: datetime) -> Beat:
        if not self.due(now):
            return Beat(None)
        checklist = self.checklist.read_text() if self.checklist.exists() else "No checklist yet."
        text = f"Time: {now:%A %Y-%m-%d %H:%M}\n\nChecklist:\n{checklist}\n\nState:\n{self.state()}"
        try:
            r = self.router.chat(self.model, PROMPT, [{"role": "user", "content": text}], [], private=self.private, max_tokens=300)
        except NoModelAvailable:
            return Beat(None)
        reply = r.text.strip()
        if not reply or reply.startswith(OK):
            return Beat(None)
        beat = Beat(reply.removeprefix("HEADS UP:").strip(), Urgency.HEADS_UP if reply.startswith("HEADS UP:") else Urgency.INFO)
        self.deliver(beat)
        return beat
