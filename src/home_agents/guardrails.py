"""Money guardrails (S1.8.3). Enforced by the backend, never left to the agent.

Every purchase needs your approval in the app; these rules decide whether a request
may even be shown to you, and they hold even if an approval arrives.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class PurchaseBlocked(Exception):
    pass


@dataclass(frozen=True)
class Purchase:
    merchant: str
    item: str
    amount_cad: Decimal
    category: str


@dataclass(frozen=True)
class MoneyRules:
    per_purchase_cap: Decimal
    monthly_cap: Decimal
    blocked_categories: frozenset[str]

    @classmethod
    def from_config(cls, cfg: dict) -> "MoneyRules":
        m = cfg["money"]
        if m.get("currency", "CAD") != "CAD":
            raise ValueError("caps are in CAD")
        return cls(
            per_purchase_cap=Decimal(str(m["per_purchase_cap"])),
            monthly_cap=Decimal(str(m["monthly_cap"])),
            blocked_categories=frozenset(m.get("blocked_categories", [])),
        )

    def check(self, p: Purchase, spent_this_month_cad: Decimal, approved: bool) -> None:
        """Raises PurchaseBlocked unless the purchase may go ahead."""
        if p.category in self.blocked_categories or _looks_like_gift_card(p):
            raise PurchaseBlocked(f"category {p.category!r} is never allowed")
        if p.amount_cad <= 0:
            raise PurchaseBlocked("amount must be positive")
        if p.amount_cad > self.per_purchase_cap:
            raise PurchaseBlocked(f"over the per-purchase cap of CA${self.per_purchase_cap}")
        if spent_this_month_cad + p.amount_cad > self.monthly_cap:
            raise PurchaseBlocked(f"over the monthly cap of CA${self.monthly_cap}")
        if not approved:
            raise PurchaseBlocked("needs your approval in the app")


_GIFT_CARD_WORDS = ("gift card", "giftcard", "e-gift", "egift", "prepaid card", "razer gold", "steam wallet")


def _looks_like_gift_card(p: Purchase) -> bool:
    text = f"{p.merchant} {p.item}".lower()
    return any(w in text for w in _GIFT_CARD_WORDS)
