"""Recurring agent jobs (E3.5), e.g. the monthly TTC reimbursement or a weekly price watch.

Schedules are stored as plain rules; each due run goes through the orchestrator and its
approvals exactly like a job you asked for by voice.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Schedule:
    name: str
    request: str                  # what you'd say, e.g. "make my TTC reimbursement for last month"
    every: str                    # "monthly", "weekly", "daily"
    day: int = 1                  # day of month (monthly) or weekday 0=Mon (weekly)
    hour: int = 9
    minute: int = 0


def next_run(s: Schedule, after: datetime) -> datetime:
    base = after.replace(second=0, microsecond=0)
    if s.every == "daily":
        cand = base.replace(hour=s.hour, minute=s.minute)
        return cand if cand > after else cand + timedelta(days=1)
    if s.every == "weekly":
        cand = base.replace(hour=s.hour, minute=s.minute) + timedelta(days=(s.day - base.weekday()) % 7)
        return cand if cand > after else cand + timedelta(days=7)
    if s.every == "monthly":
        y, m = base.year, base.month
        for _ in range(2):
            d = min(s.day, calendar.monthrange(y, m)[1])
            cand = datetime(y, m, d, s.hour, s.minute, tzinfo=after.tzinfo)
            if cand > after:
                return cand
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise ValueError(f"unknown schedule {s.every!r}")


def due(schedules: list[Schedule], last_runs: dict[str, datetime], now: datetime) -> list[Schedule]:
    return [s for s in schedules if next_run(s, last_runs.get(s.name, now - timedelta(days=40))) <= now]
