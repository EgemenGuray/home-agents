from decimal import Decimal

import pytest
import yaml

from home_agents.egress import RouteMismatch, verify_route
from home_agents.guardrails import MoneyRules, Purchase, PurchaseBlocked
from home_agents.policy import MessagePolicy

CFG = yaml.safe_load(open("config/policy.example.yaml"))


def test_messaging_allow_list():
    p = MessagePolicy.from_config(CFG)
    assert p.allows("orchestrator", "researcher")
    assert p.allows("researcher", "orchestrator")
    assert p.allows("human", "writer")
    assert p.allows("writer", "researcher")
    assert not p.allows("writer", "coder")
    assert not p.allows("writer", "writer")


def test_double_wildcard_never_opens_everything():
    p = MessagePolicy.from_config({"messaging": {"allow": [["*", "*"]]}})
    assert not p.allows("a", "b")


def rules():
    return MoneyRules.from_config(CFG)


def buy(amount, category="household", merchant="Shop", item="paper towels"):
    return Purchase(merchant, item, Decimal(amount), category)


def test_purchase_needs_approval_and_caps():
    rules().check(buy("20"), Decimal("0"), approved=True)
    with pytest.raises(PurchaseBlocked, match="approval"):
        rules().check(buy("20"), Decimal("0"), approved=False)
    with pytest.raises(PurchaseBlocked, match="per-purchase"):
        rules().check(buy("151"), Decimal("0"), approved=True)
    with pytest.raises(PurchaseBlocked, match="monthly"):
        rules().check(buy("100"), Decimal("350"), approved=True)


@pytest.mark.parametrize("p", [
    buy("10", category="gift_card"),
    buy("10", category="crypto"),
    buy("10", item="Razer Gold PIN"),
    buy("25", merchant="Amazon", item="Amazon.ca eGift card"),
])
def test_gift_cards_and_crypto_always_blocked(p):
    with pytest.raises(PurchaseBlocked, match="never allowed"):
        rules().check(p, Decimal("0"), approved=True)


def test_route_verification():
    routes = CFG["routes"]
    assert verify_route("nord-de", routes, lambda r: ("192.0.2.1", "de")).country == "DE"
    with pytest.raises(RouteMismatch, match="expected DE"):
        verify_route("nord-de", routes, lambda r: ("192.0.2.1", "CA"))
    with pytest.raises(RouteMismatch, match="unknown"):
        verify_route("nord-xx", routes, lambda r: ("192.0.2.1", "CA"))

    def dead(_):
        raise TimeoutError("no tunnel")

    with pytest.raises(RouteMismatch, match="unreachable"):
        verify_route("home", routes, dead)
