"""One delivery rule for everything the assistant says unprompted (S1.11.1).

Urgent goes to the room you are in, everything else to your phone, nothing spoken during
quiet hours, and safety alerts always get through everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum


class Urgency(int, Enum):
    INFO = 0        # package delivered, news
    HEADS_UP = 1    # leave in 10 minutes, rain coming, door left open
    URGENT = 2      # card charge you didn't expect, leak while away
    SAFETY = 3      # smoke, CO, leak


@dataclass(frozen=True)
class Delivery:
    speak_in: tuple[str, ...]
    push: bool
    wait_until_morning: bool = False
    critical: bool = False


@dataclass(frozen=True)
class QuietHours:
    start: time = time(22, 30)
    end: time = time(7, 0)
    rooms: tuple[str, ...] = ("bedroom",)

    def active(self, now: datetime) -> bool:
        t = now.time()
        if self.start > self.end:  # spans midnight
            return t >= self.start or t < self.end
        return self.start <= t < self.end


def decide(urgency: Urgency, now: datetime, *, home: bool, room: str | None, all_rooms: tuple[str, ...],
           quiet: QuietHours = QuietHours()) -> Delivery:
    if urgency is Urgency.SAFETY:
        return Delivery(all_rooms if home else (), push=True, critical=True)
    is_quiet = quiet.active(now)
    if urgency is Urgency.URGENT:
        speak = (room,) if home and room and not (is_quiet and room in quiet.rooms) else ()
        return Delivery(speak, push=True, critical=is_quiet)
    if urgency is Urgency.HEADS_UP:
        if not home:
            return Delivery((), push=True)
        if is_quiet:
            return Delivery((), push=False, wait_until_morning=True)
        return Delivery((room,) if room else (), push=room is None)
    # INFO never speaks; it waits for the phone or the next briefing
    return Delivery((), push=not is_quiet, wait_until_morning=is_quiet)
