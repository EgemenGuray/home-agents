"""Prometheus metrics for the agent service and harness (monitoring stack in home-infra).

Plain text exposition format, no extra dependency. Served at /metrics behind the service token;
Prometheus on the hub Mac reads the token from a file. Only counts and money, never content.
"""

from __future__ import annotations

import time


def _line(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    if labels:
        inner = ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
                         for k, v in sorted(labels.items()))
        return f"{name}{{{inner}}} {value}"
    return f"{name} {value}"


def render(conn, spend, approvals=None, harness=None) -> str:
    out = [
        "# HELP home_agents_up The agent service answered this scrape.",
        "# TYPE home_agents_up gauge",
        _line("home_agents_up", 1),
        "# HELP home_agents_spend_cad Model spend in CAD for the current period.",
        "# TYPE home_agents_spend_cad gauge",
        _line("home_agents_spend_cad", round(spend.today(), 4), {"period": "day"}),
        _line("home_agents_spend_cad", round(spend.month(), 4), {"period": "month"}),
        "# HELP home_agents_spend_limit_cad Daily alert threshold and monthly hard cap, in CAD.",
        "# TYPE home_agents_spend_limit_cad gauge",
        _line("home_agents_spend_limit_cad", spend.daily_alert_cad, {"period": "day"}),
        _line("home_agents_spend_limit_cad", spend.monthly_cap_cad, {"period": "month"}),
    ]

    pending = approvals.pending() if approvals else []
    oldest = max((time.time() - r.created_at for r in pending), default=0)
    out += [
        "# HELP home_agents_approvals_pending Approval requests waiting on the phone.",
        "# TYPE home_agents_approvals_pending gauge",
        _line("home_agents_approvals_pending", len(pending)),
        "# HELP home_agents_approval_oldest_seconds Age of the oldest waiting approval.",
        "# TYPE home_agents_approval_oldest_seconds gauge",
        _line("home_agents_approval_oldest_seconds", round(oldest)),
    ]

    if harness is not None:
        from .harness.store import TASK_STATES

        counts = dict(conn.execute("SELECT state, COUNT(*) FROM tasks GROUP BY state").fetchall())
        out += ["# HELP home_agents_tasks Harness tasks by state (all time).", "# TYPE home_agents_tasks gauge"]
        out += [_line("home_agents_tasks", counts.get(s, 0), {"state": s}) for s in TASK_STATES]
        out += ["# HELP home_agents_provider_available Model provider is reachable.",
                "# TYPE home_agents_provider_available gauge"]
        out += [_line("home_agents_provider_available", int(bool(p["available"])),
                      {"provider": p["provider"], "location": p["location"]}) for p in harness.router.status()]
    return "\n".join(out) + "\n"
