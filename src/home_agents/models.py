"""Model routing (S1.1.2), spend accounting in CAD (S1.8.2) and local fallback (S2.3.2)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import Enum

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
LOCAL = "local"

# USD per million tokens (input, output), Anthropic first-party rates.
PRICES_USD = {HAIKU: (0.10, 0.50), SONNET: (2.00, 10.00), OPUS: (4.00, 20.00), LOCAL: (0.0, 0.0)}


class Purpose(str, Enum):
    ROUTING = "routing"      # intent routing, summaries
    DEFAULT = "default"      # everyday answers and device control
    PLANNING = "planning"    # splitting a job into subtasks, hard reasoning


@dataclass(frozen=True)
class Route:
    model: str
    reason: str


_PRIVATE = re.compile(r"\b(private|privately|offline|don't send|do not send|keep (it|this) (local|home))\b", re.I)


def choose_model(purpose: Purpose, text: str = "", *, cloud_up: bool = True, local_available: bool = False) -> Route:
    """Cloud Claude is the decision maker; the local model takes private requests and outages (Phase 2)."""
    if local_available and _PRIVATE.search(text):
        return Route(LOCAL, "private request stays on the local model")
    if not cloud_up:
        if local_available:
            return Route(LOCAL, "cloud unavailable, local fallback")
        raise RuntimeError("cloud unavailable and no local model; queue the request")
    if purpose is Purpose.ROUTING:
        return Route(HAIKU, "routing")
    if purpose is Purpose.PLANNING:
        return Route(OPUS, "planning")
    return Route(SONNET, "default")


def cost_cad(model: str, input_tokens: int, output_tokens: int, usd_to_cad: float,
             price_usd: tuple[float, float] | None = None) -> float:
    pin, pout = price_usd or PRICES_USD.get(model, PRICES_USD[OPUS])
    return (input_tokens * pin + output_tokens * pout) / 1_000_000 * usd_to_cad


class SpendExceeded(Exception):
    pass


class Spend:
    def __init__(self, conn: sqlite3.Connection, usd_to_cad: float, daily_alert_cad: float, monthly_cap_cad: float):
        self.conn = conn
        self.usd_to_cad = usd_to_cad
        self.daily_alert_cad = daily_alert_cad
        self.monthly_cap_cad = monthly_cap_cad

    def record(self, model: str, input_tokens: int, output_tokens: int, job_id: str | None = None,
               price_usd: tuple[float, float] | None = None) -> float:
        c = cost_cad(model, input_tokens, output_tokens, self.usd_to_cad, price_usd)
        self.conn.execute(
            "INSERT INTO spend(model, input_tokens, output_tokens, cost_cad, job_id) VALUES (?, ?, ?, ?, ?)",
            (model, input_tokens, output_tokens, c, job_id),
        )
        return c

    def today(self) -> float:
        return self.conn.execute("SELECT COALESCE(SUM(cost_cad), 0) FROM spend WHERE date(created_at) = date('now')").fetchone()[0]

    def month(self) -> float:
        return self.conn.execute(
            "SELECT COALESCE(SUM(cost_cad), 0) FROM spend WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')"
        ).fetchone()[0]

    def check_before_call(self) -> None:
        if self.month() >= self.monthly_cap_cad:
            raise SpendExceeded(f"monthly cap of CA${self.monthly_cap_cad:.0f} reached")

    def daily_alert_due(self) -> bool:
        return self.today() >= self.daily_alert_cad
