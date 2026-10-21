"""Runtime settings, read from the environment (an ignored .env on the server).

Nothing secret has a default. The Anthropic key is read by the SDK itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value != "" else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("HOME_AGENTS_DATA", "./data")))
    policy_file: Path = field(default_factory=lambda: Path(_env("HOME_AGENTS_POLICY", "config/policy.yaml")))
    # Home Assistant, reached over the LAN or tailnet with a scoped, non-admin user's token.
    ha_url: str | None = field(default_factory=lambda: _env("HA_URL"))
    ha_token: str | None = field(default_factory=lambda: _env("HA_TOKEN"))
    # Local model server (Phase 2, on the Mac Studio): an HTTP endpoint for llama.cpp or MLX.
    local_llm_url: str | None = field(default_factory=lambda: _env("LOCAL_LLM_URL"))
    # Spend, in CAD.
    daily_alert_cad: float = field(default_factory=lambda: float(_env("DAILY_SPEND_ALERT_CAD", "5")))
    monthly_cap_cad: float = field(default_factory=lambda: float(_env("MONTHLY_SPEND_CAP_CAD", "70")))
    usd_to_cad: float = field(default_factory=lambda: float(_env("USD_TO_CAD", "1.40")))
    # Per-request token budget (input + output across the whole tool loop).
    request_token_budget: int = field(default_factory=lambda: int(_env("REQUEST_TOKEN_BUDGET", "60000")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "home_agents.sqlite3"
