"""Which model answers (E1.12): named chains, private mode, failover and spend.

A chain is an ordered list of "provider:model" entries under an alias (fast, default,
planning, private, or any name a profile uses). The router walks it: skips providers that
are down or missing a key, skips cloud providers in private mode, and moves on when a call
fails with ProviderDown. Cloud calls stop at the monthly cap; local calls cost nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..audit import AuditLog
from ..models import Spend, SpendExceeded
from .providers import ChatResult, Provider, ProviderDown


class NoModelAvailable(Exception):
    pass


@dataclass(frozen=True)
class Choice:
    provider: str
    model: str


def parse_entry(entry: str) -> Choice:
    provider, _, model = entry.partition(":")
    if not model:
        raise ValueError(f"model entry {entry!r} must look like provider:model")
    return Choice(provider, model)


class ModelRouter:
    def __init__(self, providers: dict[str, Provider], chains: dict[str, list[str]], spend: Spend | None = None,
                 audit: AuditLog | None = None, prices_usd: dict[str, list[float]] | None = None):
        self.providers = providers
        self.chains = {alias: [parse_entry(e) for e in entries] for alias, entries in (chains or {}).items()}
        for alias, entries in self.chains.items():
            for c in entries:
                if c.provider not in providers:
                    raise ValueError(f"chain {alias}: unknown provider {c.provider!r}")
        self.spend = spend
        self.audit = audit
        self.prices = {k: tuple(v) for k, v in (prices_usd or {}).items()}

    def candidates(self, alias: str, private: bool = False) -> list[Choice]:
        if alias not in self.chains:
            alias = "default"
        chain = self.chains.get(alias, [])
        if private:
            chain = [c for c in chain if self.providers[c.provider].location == "local"] or \
                [c for c in self.chains.get("private", []) if self.providers[c.provider].location == "local"]
        return chain

    def chat(self, alias: str, system: str, messages: list[dict], tools: list[dict], *, private: bool = False,
             max_tokens: int = 8000, job_id: str | None = None) -> ChatResult:
        tried = []
        for c in self.candidates(alias, private):
            p = self.providers[c.provider]
            if not p.available():
                tried.append(f"{c.provider}:{c.model} unavailable")
                continue
            if p.location == "cloud" and self.spend is not None:
                try:
                    self.spend.check_before_call()
                except SpendExceeded as exc:  # cloud is off for the month; local entries later in the chain still run
                    tried.append(f"{c.provider}:{c.model} skipped ({exc})")
                    continue
            try:
                result = p.chat(c.model, system, messages, tools, max_tokens)
            except ProviderDown as exc:
                tried.append(f"{c.provider}:{c.model} down ({exc})")
                if self.audit:
                    self.audit.record("model_failover", "harness", {"model": f"{c.provider}:{c.model}", "error": str(exc)[:200]}, job_id)
                continue
            if self.spend is not None:
                price = (0.0, 0.0) if p.location == "local" else self.prices.get(result.model)
                self.spend.record(result.model, result.input_tokens, result.output_tokens, job_id, price)
            return result
        raise NoModelAvailable("; ".join(tried) or f"no models configured for {alias!r}" + (" in private mode" if private else ""))

    def status(self) -> list[dict]:
        return [{"provider": name, "location": p.location, "available": p.available()} for name, p in self.providers.items()]
