"""Routines (E1.12): Alexa Routines and iPhone Shortcuts automations, run by the household agent.

A routine is "when" (triggers), "only if" (conditions) and "do" (steps):

    name: Morning
    triggers:
      - {type: phrase, phrases: ["good morning"]}
      - {type: time, at: "06:45", days: weekdays}
    conditions:
      - {type: state, entity: person.owner, is: home}
    steps:
      - {device: {domain: light, service: turn_on, area_id: kitchen, data: {brightness_pct: 60}}}
      - {ask_agent: "Today's weather and my first meeting, in two sentences", save_as: brief}
      - {say: "{brief}", room: kitchen}
    cooldown_minutes: 60

Triggers   phrase, time (at, days, date), interval, sun (sunrise or sunset with an offset),
           state (to, from, above, below, for_minutes), presence (arrive or leave a zone),
           event and tag (HA events, NFC tags, phone sensors), webhook, manual (app, console,
           CLI, an iPhone Shortcut), harness (task_done, task_failed, memory_sleep)
Conditions time window, days, state, someone_home, sun up or down, var
Steps      device, say, notify, wait, ask_agent, task, confirm, if/then/else, set, run_routine, stop

Device steps go through the agent's own tool path, so a lock, the alarm or the garage still asks
for your phone's approval, and household hook rules still apply. A routine the agent writes is
saved switched off and only turns on when you approve it on the phone.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from ..tools import Tool
from .hooks import fill
from .loop import Hooks, RunResult
from .providers import ToolCall
from .store import new_id

TRIGGERS = ("phrase", "time", "interval", "sun", "state", "presence", "event", "tag", "webhook", "manual", "harness")
CONDITIONS = ("time", "days", "state", "someone_home", "sun", "var")
STEPS = ("device", "say", "notify", "wait", "ask_agent", "task", "confirm", "if", "set", "run_routine", "stop")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_SETS = {"daily": DAYS, "weekdays": DAYS[:5], "weekends": DAYS[5:]}
MAX_STEPS = 200          # per run, counting nested ones: a routine can't spin forever
MAX_NESTING = 3          # run_routine depth

SCHEMA = """
CREATE TABLE IF NOT EXISTS routines (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, definition TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL DEFAULT 'owner', approval_id TEXT, last_run TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS routine_runs (
  id INTEGER PRIMARY KEY, routine_id TEXT NOT NULL, trigger TEXT NOT NULL, state TEXT NOT NULL,
  steps TEXT NOT NULL DEFAULT '[]', vars TEXT NOT NULL DEFAULT '{}', started_at TEXT NOT NULL, finished_at TEXT
);
"""


class RoutineError(ValueError):
    pass


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


def _days(spec) -> tuple[str, ...]:
    if spec is None:
        return DAYS
    if isinstance(spec, str):
        if spec in DAY_SETS:
            return DAY_SETS[spec]
        spec = [s.strip() for s in spec.split(",")]
    out = tuple(d[:3].lower() for d in spec)
    if any(d not in DAYS for d in out):
        raise RoutineError(f"bad days {spec}")
    return out


def _hm(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text).strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise RoutineError(f"bad time {text!r}; use HH:MM")
    return int(m[1]), int(m[2])


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Sunrise and sunset (NOAA's simplified algorithm, good to about a minute) --------------------------

def sun_times(day: date, lat: float, lon: float) -> tuple[datetime | None, datetime | None]:
    """UTC sunrise and sunset for a day; None at the poles when the sun doesn't rise or set."""
    n = day.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma) - 0.006758 * math.cos(2 * gamma)
            + 0.000907 * math.sin(2 * gamma) - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    phi = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl)) - math.tan(phi) * math.tan(decl)
    if not -1 <= cos_ha <= 1:
        return None, None
    ha = math.degrees(math.acos(cos_ha))
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    rise = midnight + timedelta(minutes=720 - 4 * (lon + ha) - eqtime)
    set_ = midnight + timedelta(minutes=720 - 4 * (lon - ha) - eqtime)
    return rise, set_


# Validation -------------------------------------------------------------------------------------

def validate(defn: dict) -> dict:
    """Checks a routine definition and returns it normalised. Raises RoutineError with the reason."""
    if not isinstance(defn, dict) or not str(defn.get("name", "")).strip():
        raise RoutineError("a routine needs a name")
    triggers = defn.get("triggers") or []
    steps = defn.get("steps") or []
    if not triggers:
        raise RoutineError("a routine needs at least one trigger (use {type: manual} for button-only)")
    if not steps:
        raise RoutineError("a routine needs at least one step")
    for t in triggers:
        kind = t.get("type")
        if kind not in TRIGGERS:
            raise RoutineError(f"unknown trigger {kind!r}; one of {', '.join(TRIGGERS)}")
        if kind == "phrase" and not [p for p in t.get("phrases", []) if _norm(p)]:
            raise RoutineError("a phrase trigger needs phrases")
        if kind == "time":
            _hm(t.get("at", ""))
            _days(t.get("days"))
            if t.get("date"):
                date.fromisoformat(t["date"])
        if kind == "interval" and not (_num(t.get("minutes")) or 0) >= 1:
            raise RoutineError("an interval trigger needs minutes >= 1")
        if kind == "sun" and t.get("event") not in ("sunrise", "sunset"):
            raise RoutineError("a sun trigger needs event: sunrise or sunset")
        if kind in ("state", "presence") and not (t.get("entity") or t.get("person")):
            raise RoutineError(f"a {kind} trigger needs an entity")
        if kind == "presence" and t.get("event") not in ("arrive", "leave"):
            raise RoutineError("a presence trigger needs event: arrive or leave")
        if kind == "event" and not t.get("event_type"):
            raise RoutineError("an event trigger needs event_type")
        if kind == "tag" and not t.get("tag_id"):
            raise RoutineError("a tag trigger needs tag_id")
        if kind == "webhook" and not re.fullmatch(r"[a-z0-9_-]{3,40}", str(t.get("name", ""))):
            raise RoutineError("a webhook trigger needs a name of 3-40 lowercase letters, digits, - or _")
        if kind == "harness" and t.get("event") not in ("task_done", "task_failed", "memory_sleep"):
            raise RoutineError("a harness trigger needs event: task_done, task_failed or memory_sleep")
    for c in defn.get("conditions") or []:
        _check_condition(c)
    _check_steps(steps)
    return {**defn, "name": defn["name"].strip()}


def _check_condition(c: dict) -> None:
    kind = c.get("type")
    if kind not in CONDITIONS:
        raise RoutineError(f"unknown condition {kind!r}; one of {', '.join(CONDITIONS)}")
    if kind == "time":
        for k in ("after", "before"):
            if c.get(k):
                _hm(c[k])
    if kind == "days":
        _days(c.get("days"))
    if kind == "state" and not c.get("entity"):
        raise RoutineError("a state condition needs an entity")
    if kind == "sun" and c.get("is") not in ("up", "down"):
        raise RoutineError("a sun condition needs is: up or down")
    if kind == "var" and not c.get("name"):
        raise RoutineError("a var condition needs a name")


def _check_steps(steps: list) -> None:
    for s in steps:
        if not isinstance(s, dict) or len([k for k in s if k in STEPS]) != 1:
            raise RoutineError(f"each step needs exactly one of {', '.join(STEPS)}: {s}")
        if "device" in s and not (s["device"].get("domain") and s["device"].get("service")):
            raise RoutineError("a device step needs domain and service")
        if "wait" in s and not 0 <= (_num(s["wait"]) or -1) <= 3600:
            raise RoutineError("wait is in seconds, up to 3600")
        if "if" in s:
            for c in s["if"] if isinstance(s["if"], list) else [s["if"]]:
                _check_condition(c)
            _check_steps(s.get("then") or [])
            _check_steps(s.get("else") or [])


# The engine -------------------------------------------------------------------------------------

@dataclass
class Run:
    routine_id: str
    trigger: dict
    state: str = "running"
    steps: list[dict] = field(default_factory=list)
    vars: dict = field(default_factory=dict)
    spoken: list[str] = field(default_factory=list)
    id: int = 0


class _Stop(Exception):
    pass


class RoutineEngine:
    """Owns routine definitions and decides when they fire. The daemon feeds it the clock and HA events.

    `run_tool(name, input, job_id)` runs a tool through the agent's approval and hook path and
    returns (text, is_error). `ask_agent(prompt, profile)` returns the agent's answer.
    """

    def __init__(self, conn, *, run_tool: Callable[[str, dict, str], tuple[str, bool]],
                 ask_agent: Callable[[str, str], str] = lambda prompt, profile: "",
                 start_task: Callable[..., dict] | None = None, notify: Callable[[str], None] = lambda text: None,
                 approvals=None, audit=None, states: Callable[[], dict[str, str]] = dict,
                 location: dict | None = None, sleep: Callable[[float], None] = time.sleep,
                 say: dict | None = None, confirm_timeout: float = 300, poll_seconds: float = 2):
        self.conn = conn
        conn.executescript(SCHEMA)
        self.run_tool = run_tool
        self.ask_agent = ask_agent
        self.start_task = start_task
        self.notify = notify
        self.approvals = approvals
        self.audit = audit
        self.states = states
        self.location = location or {}
        self.sleep = sleep
        # How "say" reaches a speaker: an HA script that takes message and room (packages/announce.yaml).
        self.say_cfg = {"domain": "script", "service": "announce", **(say or {})}
        self.confirm_timeout = confirm_timeout
        self.poll_seconds = poll_seconds
        self._fired: dict[str, str] = {}          # trigger key -> last minute or day it fired
        self._held: dict[str, datetime] = {}      # state trigger with for_minutes -> when it started holding
        self._last: dict[str, str] = {}           # entity -> last state seen, for from/to and crossings

    # Definitions ----------------------------------------------------------------------------------

    def _row(self, r) -> dict:
        d = json.loads(r["definition"])
        return {**d, "id": r["id"], "enabled": bool(r["enabled"]), "created_by": r["created_by"],
                "approval_id": r["approval_id"], "last_run": r["last_run"]}

    def add(self, defn: dict, created_by: str = "owner") -> dict:
        d = validate({k: v for k, v in defn.items() if k not in ("id", "enabled", "created_by", "approval_id", "last_run")})
        if self.by_name(d["name"]):
            raise RoutineError(f"a routine called {d['name']!r} already exists")
        rid = new_id("rtn")
        # The agent's drafts start switched off; you turn them on with a phone approval.
        enabled = created_by == "owner" and defn.get("enabled", True) is not False
        self.conn.execute("INSERT INTO routines(id, name, definition, enabled, created_by) VALUES (?, ?, ?, ?, ?)",
                          (rid, d["name"], json.dumps(d), int(enabled), created_by))
        if created_by != "owner" and self.approvals is not None:
            req = self.approvals.request("routine", f"Turn on routine '{d['name']}': {describe(d)}", {"routine": d},
                                         on_approve=lambda _r: self.set_enabled(rid, True, by="phone"))
            self.conn.execute("UPDATE routines SET approval_id = ? WHERE id = ?", (req.id, rid))
        self._audit("routine_created", created_by, {"id": rid, "name": d["name"], "enabled": enabled})
        return self.get(rid)

    def update(self, rid: str, defn: dict, by: str = "owner") -> dict:
        cur = self.get(rid)
        if cur is None:
            raise KeyError(rid)
        if by != "owner":
            raise RoutineError("only the owner can change a routine; ask the agent to draft a new one instead")
        d = validate({k: v for k, v in defn.items() if k not in ("id", "enabled", "created_by", "approval_id", "last_run")})
        self.conn.execute("UPDATE routines SET name = ?, definition = ? WHERE id = ?", (d["name"], json.dumps(d), rid))
        self._audit("routine_updated", by, {"id": rid})
        return self.get(rid)

    def delete(self, rid: str, by: str = "owner") -> bool:
        n = self.conn.execute("DELETE FROM routines WHERE id = ?", (rid,)).rowcount
        if n:
            self._audit("routine_deleted", by, {"id": rid})
        return n > 0

    def set_enabled(self, rid: str, on: bool, by: str = "owner") -> bool:
        r = self.get(rid)
        if r is None:
            return False
        if on and r["created_by"] != "owner" and by not in ("owner", "phone"):
            return False
        self.conn.execute("UPDATE routines SET enabled = ? WHERE id = ?", (int(on), rid))
        self._audit("routine_enabled" if on else "routine_disabled", by, {"id": rid})
        return True

    def get(self, rid: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM routines WHERE id = ?", (rid,)).fetchone()
        return self._row(r) if r else None

    def by_name(self, name: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM routines WHERE lower(name) = lower(?)", (name.strip(),)).fetchone()
        return self._row(r) if r else None

    def list(self) -> list[dict]:
        return [self._row(r) for r in self.conn.execute("SELECT * FROM routines ORDER BY name")]

    def runs(self, rid: str | None = None, limit: int = 50) -> list[dict]:
        q, args = "SELECT * FROM routine_runs", []
        if rid:
            q, args = q + " WHERE routine_id = ?", [rid]
        rows = self.conn.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit])
        return [{**dict(r), "steps": json.loads(r["steps"]), "vars": json.loads(r["vars"]), "trigger": json.loads(r["trigger"])}
                for r in rows]

    def _enabled(self) -> list[dict]:
        return [self._row(r) for r in self.conn.execute("SELECT * FROM routines WHERE enabled = 1 ORDER BY name")]

    def _audit(self, kind: str, actor: str, data: dict) -> None:
        if self.audit:
            self.audit.record(kind, actor, data)

    # Triggers -------------------------------------------------------------------------------------

    def match_phrase(self, text: str) -> tuple[dict, str] | None:
        """A spoken or typed request that is one of a routine's phrases (near-exact), before any model runs."""
        said = _norm(text)
        if not said:
            return None
        best, best_score = None, 0.0
        for r in self._enabled():
            for t in r["triggers"]:
                if t["type"] != "phrase":
                    continue
                for p in t["phrases"]:
                    score = 1.0 if _norm(p) == said else difflib.SequenceMatcher(None, _norm(p), said).ratio()
                    if score > best_score:
                        best, best_score = (r, p), score
        return best if best and best_score >= float(0.9) else None

    def tick(self, now: datetime) -> list[Run]:
        """Called every ~30 s with local time (aware). Fires time, interval and sun triggers once each,
        and state triggers whose for_minutes has passed."""
        out = []
        for r in self._enabled():
            for i, t in enumerate(r["triggers"]):
                key = f"{r['id']}:{i}"
                if t["type"] == "time" and self._time_due(t, now, key):
                    out.append(self._fire(r, {"type": "time", "at": t["at"]}, now))
                elif t["type"] == "interval":
                    last = self._fired.get(key)
                    if last is None or now - datetime.fromisoformat(last) >= timedelta(minutes=float(t["minutes"])):
                        if last is not None:
                            out.append(self._fire(r, {"type": "interval"}, now))
                        self._fired[key] = now.isoformat()
                elif t["type"] == "sun" and self._sun_due(t, now, key):
                    out.append(self._fire(r, {"type": "sun", "event": t["event"]}, now))
                elif t["type"] in ("state", "presence") and key in self._held:
                    since = self._held[key]
                    if now - since >= timedelta(minutes=float(t.get("for_minutes", 0))):
                        del self._held[key]
                        out.append(self._fire(r, {"type": t["type"], "entity": t.get("entity") or t.get("person"),
                                                  "to": self._last.get(t.get("entity") or t.get("person"))}, now))
        return [x for x in out if x is not None]

    def _time_due(self, t: dict, now: datetime, key: str) -> bool:
        h, m = _hm(t["at"])
        if t.get("date") and now.date().isoformat() != t["date"]:
            return False
        if DAYS[now.weekday()] not in _days(t.get("days")):
            return False
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        # Due in the two minutes after the target, once per day, so a slow tick never skips it.
        if not target <= now < target + timedelta(minutes=2) or self._fired.get(key) == now.date().isoformat():
            return False
        self._fired[key] = now.date().isoformat()
        return True

    def _sun_due(self, t: dict, now: datetime, key: str) -> bool:
        lat, lon = self.location.get("latitude"), self.location.get("longitude")
        if lat is None or lon is None:
            return False
        rise, set_ = sun_times(now.date(), float(lat), float(lon))
        at = rise if t["event"] == "sunrise" else set_
        if at is None:
            return False
        at = at + timedelta(minutes=float(t.get("offset_minutes", 0)))
        if not at <= now.astimezone(timezone.utc) < at + timedelta(minutes=2) or self._fired.get(key) == now.date().isoformat():
            return False
        self._fired[key] = now.date().isoformat()
        return True

    def on_state(self, entity: str, new: str, now: datetime, old: str | None = None) -> list[Run]:
        """A device, sensor or person changed (from the HA poller or a pushed event)."""
        old = self._last.get(entity) if old is None else old
        self._last[entity] = new
        if old == new:
            return []
        out = []
        for r in self._enabled():
            for i, t in enumerate(r["triggers"]):
                if t["type"] not in ("state", "presence") or (t.get("entity") or t.get("person")) != entity:
                    continue
                key = f"{r['id']}:{i}"
                hit = self._state_hit(t, old, new)
                if not hit:
                    self._held.pop(key, None)
                    continue
                if float(t.get("for_minutes", 0)) > 0:
                    self._held.setdefault(key, now)  # fires from tick() once it has held long enough
                    continue
                out.append(self._fire(r, {"type": t["type"], "entity": entity, "from": old, "to": new}, now))
        return [x for x in out if x is not None]

    @staticmethod
    def _state_hit(t: dict, old: str | None, new: str) -> bool:
        if t["type"] == "presence":
            zone = t.get("zone", "home")
            return new == zone and old != zone if t["event"] == "arrive" else old == zone and new != zone
        if "to" in t and str(t["to"]) != new:
            return False
        if "from" in t and str(t["from"]) != old:
            return False
        n, o = _num(new), _num(old)
        if "above" in t:
            if n is None or n <= float(t["above"]) or (o is not None and o > float(t["above"])):
                return False  # only the crossing counts, not every reading above the line
        if "below" in t:
            if n is None or n >= float(t["below"]) or (o is not None and o < float(t["below"])):
                return False
        return True

    def on_event(self, event_type: str, data: dict, now: datetime) -> list[Run]:
        """HA events (tag_scanned, mobile_app_notification_action, a phone's alarm...), webhooks and harness events."""
        out = []
        for r in self._enabled():
            for t in r["triggers"]:
                kind = t["type"]
                if kind == "event" and t["event_type"] == event_type and all(str(data.get(k)) == str(v) for k, v in (t.get("data") or {}).items()):
                    out.append(self._fire(r, {"type": "event", "event_type": event_type, "data": data}, now))
                elif kind == "tag" and event_type == "tag_scanned" and str(data.get("tag_id")) == str(t["tag_id"]):
                    out.append(self._fire(r, {"type": "tag", "tag_id": t["tag_id"]}, now))
                elif kind == "webhook" and event_type == f"webhook:{t['name']}":
                    out.append(self._fire(r, {"type": "webhook", "name": t["name"], "data": data}, now))
                elif kind == "harness" and event_type == t["event"]:
                    out.append(self._fire(r, {"type": "harness", "event": event_type, "data": data}, now))
        return [x for x in out if x is not None]

    def _fire(self, r: dict, trigger: dict, now: datetime) -> Run | None:
        cooldown = float(r.get("cooldown_minutes", 0))
        if cooldown and r.get("last_run"):
            if now - datetime.fromisoformat(r["last_run"]) < timedelta(minutes=cooldown):
                return None
        return self.run(r["id"], trigger, now=now)

    # Conditions -----------------------------------------------------------------------------------

    def check(self, conditions: list[dict], now: datetime, vars: dict | None = None) -> tuple[bool, str]:
        states = None
        for c in conditions or []:
            kind = c["type"]
            ok = True
            if kind == "time":
                cur = (now.hour, now.minute)
                after, before = _hm(c["after"]) if c.get("after") else None, _hm(c["before"]) if c.get("before") else None
                if after and before and after > before:   # a window across midnight, e.g. 22:00-06:00
                    ok = cur >= after or cur < before
                else:
                    ok = (after is None or cur >= after) and (before is None or cur < before)
            elif kind == "days":
                ok = DAYS[now.weekday()] in _days(c.get("days"))
            elif kind in ("state", "someone_home"):
                states = states if states is not None else self.states()
                if kind == "someone_home":
                    ok = any(v == "home" for k, v in states.items() if k.startswith("person."))
                else:
                    val = states.get(c["entity"])
                    if "is" in c:
                        ok = val == str(c["is"])
                    if "is_not" in c:
                        ok = ok and val != str(c["is_not"])
                    if "above" in c:
                        ok = ok and (_num(val) or float("-inf")) > float(c["above"])
                    if "below" in c:
                        ok = ok and (_num(val) if _num(val) is not None else float("inf")) < float(c["below"])
            elif kind == "sun":
                lat, lon = self.location.get("latitude"), self.location.get("longitude")
                if lat is None or lon is None:
                    return False, "sun condition needs routines.location"
                rise, set_ = sun_times(now.date(), float(lat), float(lon))
                utc = now.astimezone(timezone.utc)
                up = rise is not None and rise <= utc < set_
                ok = up if c["is"] == "up" else not up
            elif kind == "var":
                val = str((vars or {}).get(c["name"], "")).lower()
                if "equals" in c:
                    ok = val.strip() == str(c["equals"]).lower()
                if "contains" in c:
                    ok = ok and str(c["contains"]).lower() in val
            if c.get("not"):
                ok = not ok
            if not ok:
                return False, f"{kind} condition not met"
        return True, ""

    # Running --------------------------------------------------------------------------------------

    def run(self, rid: str, trigger: dict | None = None, *, now: datetime | None = None, dry_run: bool = False,
            depth: int = 0, vars: dict | None = None) -> Run:
        r = self.get(rid)
        if r is None:
            raise KeyError(rid)
        now = now or datetime.now().astimezone()
        trigger = trigger or {"type": "manual"}
        scalars = lambda d: {k: v for k, v in (d or {}).items() if isinstance(v, (str, int, float))}  # noqa: E731
        # Event data (a task's title, a webhook's fields) can fill templates; it is text, never steps.
        run = Run(rid, trigger, vars={**(vars or {}), **scalars(trigger.get("data")), **scalars(trigger),
                                      "trigger": trigger.get("type", ""), "routine": r["name"]})
        ok, why = self.check(r.get("conditions") or [], now, run.vars)
        if not ok:
            run.state = "skipped"
            run.steps.append({"step": "conditions", "result": why})
            return self._save(run, now, record=trigger.get("type") == "manual")
        if not dry_run:
            self.conn.execute("UPDATE routines SET last_run = ? WHERE id = ?", (now.isoformat(), rid))
        self._audit("routine_run", trigger.get("type", "manual"), {"id": rid, "name": r["name"], "dry_run": dry_run})
        try:
            self._steps(r["steps"], run, now, dry_run, depth, [0])
            run.state = "done"
        except _Stop:
            run.state = "done"
        except Exception as exc:  # a broken step ends this run, never the engine
            run.state = "failed"
            run.steps.append({"step": "error", "result": str(exc)[:500]})
            if depth == 0:  # the outermost routine reports once
                self.notify(f"Routine '{r['name']}' failed: {str(exc)[:200]}")
        return self._save(run, now)

    def _save(self, run: Run, now: datetime, record: bool = True) -> Run:
        if record:
            cur = self.conn.execute("INSERT INTO routine_runs(routine_id, trigger, state, steps, vars, started_at, finished_at) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (run.routine_id, json.dumps(run.trigger, default=str), run.state, json.dumps(run.steps, default=str),
                                     json.dumps(run.vars, default=str), now.isoformat(), datetime.now().astimezone().isoformat()))
            run.id = int(cur.lastrowid)
        return run

    def _steps(self, steps: list[dict], run: Run, now: datetime, dry_run: bool, depth: int, count: list[int]) -> None:
        for s in steps:
            count[0] += 1
            if count[0] > MAX_STEPS:
                raise RoutineError(f"stopped after {MAX_STEPS} steps")
            kind = next(k for k in s if k in STEPS)
            v = run.vars
            result: object = None
            if kind == "device":
                d = s["device"]
                inp = {k: (fill(x, v) if isinstance(x, str) else x) for k, x in d.items()}
                result = "(dry run)" if dry_run else self._tool("ha_call_service", inp, run)
            elif kind == "say":
                text = fill(str(s["say"]), v)
                run.spoken.append(text)
                if s.get("room") or s.get("speaker", True) is not False:
                    data = {"message": text, **({"room": fill(s["room"], v)} if s.get("room") else {})}
                    result = "(dry run)" if dry_run else self._tool("ha_call_service", {**self.say_cfg, "data": data}, run)
            elif kind == "notify":
                text = fill(str(s["notify"]), v)
                if not dry_run:
                    self.notify(text)
                result = text
            elif kind == "wait":
                if not dry_run:
                    self.sleep(float(s["wait"]))
                result = f"waited {s['wait']}s"
            elif kind == "ask_agent":
                result = "(dry run)" if dry_run else self.ask_agent(fill(str(s["ask_agent"]), v), s.get("profile", "assistant"))
                v[s.get("save_as", "last")] = result
                v["last"] = result
            elif kind == "task":
                if self.start_task is None:
                    raise RoutineError("background tasks are not available")
                if not dry_run:
                    t = self.start_task(fill(str(s["task"]), v), profile=s.get("profile", "assistant"),
                                        title=s.get("title", f"Routine: {v['routine']}"), source=f"rule:routine:{v['routine']}",
                                        mode=s.get("mode", "auto"))
                    result = t["id"]
                    v[s.get("save_as", "task_id")] = t["id"]
            elif kind == "confirm":
                answer = "approved" if dry_run else self._confirm(fill(str(s["confirm"]), v), run)
                result = answer
                if answer != "approved":
                    run.steps.append({"step": "confirm", "result": answer})
                    raise _Stop()
            elif kind == "if":
                conds = s["if"] if isinstance(s["if"], list) else [s["if"]]
                ok, _ = self.check(conds, now, v)
                result = "then" if ok else "else"
                run.steps.append({"step": "if", "result": result})
                self._steps(s.get("then" if ok else "else") or [], run, now, dry_run, depth, count)
                continue
            elif kind == "set":
                for k, x in s["set"].items():
                    v[k] = fill(x, v) if isinstance(x, str) else x
                result = "set"
            elif kind == "run_routine":
                if depth >= MAX_NESTING:
                    raise RoutineError("routines nested too deeply")
                other = self.by_name(str(s["run_routine"]))
                if other is None:
                    raise RoutineError(f"no routine {s['run_routine']!r}")
                sub = self.run(other["id"], {"type": "routine", "from": run.routine_id}, now=now, dry_run=dry_run,
                               depth=depth + 1, vars=v)
                run.spoken += sub.spoken
                if sub.state == "failed":
                    raise RoutineError(f"{other['name']}: {sub.steps[-1]['result']}")
                result = sub.state
            elif kind == "stop":
                run.steps.append({"step": "stop", "result": fill(str(s["stop"]), v) if isinstance(s["stop"], str) else ""})
                raise _Stop()
            run.steps.append({"step": kind, "result": str(result)[:1000] if result is not None else ""})

    def _tool(self, name: str, inp: dict, run: Run) -> str:
        text, is_error = self.run_tool(name, inp, f"routine:{run.routine_id}")
        if is_error:
            raise RoutineError(f"{name} failed: {text[:300]}")
        return text

    def _confirm(self, question: str, run: Run) -> str:
        """Shortcuts' "Ask before running": a yes/no on the phone, signed like any approval."""
        if self.approvals is None:
            return "no approvals set up"
        req = self.approvals.request("routine_confirm", question, {"routine": run.routine_id}, job_id=f"routine:{run.routine_id}")
        waited = 0.0
        while waited < self.confirm_timeout:
            state = (self.approvals.get(req.id).state if self.approvals.get(req.id) else "expired")
            if state != "pending":
                return state
            self.sleep(self.poll_seconds)
            waited += self.poll_seconds
        return "expired"

    # Agent tools ----------------------------------------------------------------------------------

    def tools(self) -> list[Tool]:
        obj = {"type": "object", "additionalProperties": False}

        def create(inp: dict) -> dict:
            r = self.add(inp["routine"], created_by="agent")
            return {"id": r["id"], "enabled": r["enabled"],
                    "note": "Saved switched off. The owner turns it on by approving it on their phone."}

        def run_named(inp: dict) -> dict:
            r = self.by_name(inp["name"])
            if r is None:
                return {"error": f"no routine {inp['name']!r}", "routines": [x["name"] for x in self.list()]}
            if not r["enabled"]:
                return {"error": "that routine is switched off"}
            res = self.run(r["id"], {"type": "agent"})
            return {"state": res.state, "said": res.spoken, "steps": res.steps[-5:]}

        return [
            Tool("list_routines", "List the household's routines with their triggers.", obj | {"properties": {}, "required": []},
                 lambda _i: [{"name": r["name"], "enabled": r["enabled"], "when": describe(r)} for r in self.list()]),
            Tool("run_routine", "Run a routine by name now, as if its button was pressed.",
                 obj | {"properties": {"name": {"type": "string"}}, "required": ["name"]}, run_named),
            Tool("create_routine", "Draft a new routine (when/only if/do) from what the owner asked for. It is saved "
                 "switched off until the owner approves it on their phone. Triggers: " + ", ".join(TRIGGERS) +
                 ". Steps: " + ", ".join(STEPS) + ".",
                 obj | {"properties": {"routine": {"type": "object", "properties": {
                     "name": {"type": "string"}, "triggers": {"type": "array", "items": {"type": "object"}},
                     "conditions": {"type": "array", "items": {"type": "object"}},
                     "steps": {"type": "array", "items": {"type": "object"}}, "cooldown_minutes": {"type": "number"}},
                     "required": ["name", "triggers", "steps"]}}, "required": ["routine"]}, create),
        ]


def describe(r: dict) -> str:
    """One line a person can read: "at 06:45 on weekdays, or "good morning" -> 3 steps"."""
    parts = []
    for t in r.get("triggers", []):
        k = t["type"]
        if k == "phrase":
            parts.append(" or ".join(f'"{p}"' for p in t["phrases"]))
        elif k == "time":
            days = t.get("days", "daily")
            parts.append(f"at {t['at']}" + (f" on {t['date']}" if t.get("date") else f" on {days if isinstance(days, str) else ','.join(days)}"))
        elif k == "interval":
            parts.append(f"every {t['minutes']} min")
        elif k == "sun":
            off = float(t.get("offset_minutes", 0))
            parts.append(f"{t['event']}" + (f" {off:+.0f} min" if off else ""))
        elif k in ("state", "presence"):
            ent = t.get("entity") or t.get("person")
            if k == "presence":
                parts.append(f"{ent} {t['event']}s {t.get('zone', 'home')}")
            else:
                parts.append(f"{ent} " + ", ".join(f"{x} {t[x]}" for x in ("from", "to", "above", "below", "for_minutes") if x in t))
        elif k in ("event", "tag", "webhook", "harness"):
            parts.append(f"{k} {t.get('event_type') or t.get('tag_id') or t.get('name') or t.get('event')}")
        else:
            parts.append("button")
    cond = f" if {len(r['conditions'])} condition(s)" if r.get("conditions") else ""
    return f"{' or '.join(parts)}{cond} -> {len(r.get('steps', []))} step(s)"


def tool_runner(agent, profile) -> Callable[[str, dict, str], tuple[str, bool]]:
    """Runs a routine's device steps through the agent's tool path: approvals, hooks and audit included."""
    def run_tool(name: str, inp: dict, job_id: str) -> tuple[str, bool]:
        tools = profile.tools(agent.tools)
        out = RunResult("", [])
        return agent._run_tool(tools, ToolCall(f"rt-{new_id('c')}", name, inp), job_id, Hooks(wait_for_approval=True), out, profile)
    return run_tool
