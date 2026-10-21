"""Routine suggestions (S2.7.1): spot actions you repeat by hand and offer to automate them.

Input is HA's logbook of user-initiated service calls (context.user_id set, so automations
don't count). A suggestion becomes an automation only through the E1.9 pull request flow.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Action:
    when: datetime
    entity_id: str
    service: str          # e.g. light.turn_off


@dataclass(frozen=True)
class Suggestion:
    entity_id: str
    service: str
    time: str             # HH:MM, the median time
    days: str             # weekdays, weekends, daily
    seen_on: int
    out_of: int

    def sentence(self) -> str:
        verb = self.service.split(".", 1)[1].replace("_", " ")
        return (f"You {verb} {self.entity_id} around {self.time} on {self.days} "
                f"({self.seen_on} of the last {self.out_of} days). Make it a routine?")


def suggest(actions: list[Action], days_observed: int, min_ratio: float = 0.7, window_minutes: int = 30,
            already_automated: set[tuple[str, str]] = frozenset()) -> list[Suggestion]:
    buckets: dict[tuple, list[Action]] = defaultdict(list)
    for a in actions:
        slot = (a.when.hour * 60 + a.when.minute) // window_minutes
        kind = "weekdays" if a.when.weekday() < 5 else "weekends"
        buckets[(a.entity_id, a.service, kind, slot)].append(a)

    out: dict[tuple, Suggestion] = {}
    for (entity, service, kind, _slot), acts in buckets.items():
        if (entity, service) in already_automated:
            continue
        distinct_days = {a.when.date() for a in acts}
        possible = round(days_observed * (5 / 7 if kind == "weekdays" else 2 / 7))
        if possible == 0 or len(distinct_days) / possible < min_ratio:
            continue
        minutes = sorted(a.when.hour * 60 + a.when.minute for a in acts)
        median = minutes[len(minutes) // 2]
        key = (entity, service, kind)
        s = Suggestion(entity, service, f"{median // 60:02d}:{median % 60:02d}", kind, len(distinct_days), possible)
        if key not in out or s.seen_on > out[key].seen_on:
            out[key] = s
    # weekday and weekend versions of the same habit merge into "daily"
    merged = []
    for (entity, service, kind), s in out.items():
        other = out.get((entity, service, "weekends" if kind == "weekdays" else "weekdays"))
        if other and abs(_m(other.time) - _m(s.time)) <= window_minutes:
            if kind == "weekdays":
                merged.append(Suggestion(entity, service, s.time, "daily", s.seen_on + other.seen_on, s.out_of + other.out_of))
            continue
        merged.append(s)
    return sorted(merged, key=lambda s: -s.seen_on / s.out_of)


def _m(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_automation_yaml(s: Suggestion) -> str:
    cond = {"weekdays": "[mon, tue, wed, thu, fri]", "weekends": "[sat, sun]", "daily": None}[s.days]
    lines = [
        f"- id: suggested_{s.entity_id.replace('.', '_')}_{s.service.split('.')[1]}",
        f"  alias: \"Suggested: {s.service} {s.entity_id} at {s.time}\"",
        "  triggers:",
        "    - trigger: time",
        f"      at: \"{s.time}:00\"",
    ]
    if cond:
        lines += ["  conditions:", "    - condition: time", f"      weekday: {cond}"]
    lines += ["  actions:", f"    - action: {s.service}", "      target:", f"        entity_id: {s.entity_id}"]
    return "\n".join(lines) + "\n"
