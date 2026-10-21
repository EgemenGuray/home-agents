"""Package tracking from shipping emails (S1.10.5). Gmail access is read-only."""

from __future__ import annotations

import re
from dataclasses import dataclass

CARRIERS = [
    ("Canada Post", re.compile(r"\b(\d{16}|[A-Z]{2}\d{9}CA)\b")),
    ("UPS", re.compile(r"\b(1Z[0-9A-Z]{16})\b")),
    ("Purolator", re.compile(r"\b(\d{12})\b(?=.*purolator)", re.I | re.S)),
    ("FedEx", re.compile(r"\b(\d{12}|\d{15})\b(?=.*fedex)", re.I | re.S)),
    ("DHL", re.compile(r"\b(\d{10})\b(?=.*dhl)", re.I | re.S)),
    ("Amazon", re.compile(r"\b(TBA\d{12})\b")),
]
_DELIVERED = re.compile(r"\b(delivered|has been delivered|was delivered)\b", re.I)
_OUT = re.compile(r"\b(out for delivery|arriving today)\b", re.I)


@dataclass(frozen=True)
class Shipment:
    carrier: str
    tracking: str
    status: str  # shipped, out_for_delivery, delivered


def parse_shipping_email(subject: str, body: str) -> list[Shipment]:
    text = f"{subject}\n{body}"
    status = "delivered" if _DELIVERED.search(text) else "out_for_delivery" if _OUT.search(text) else "shipped"
    found, seen = [], set()
    for carrier, pat in CARRIERS:
        for m in pat.finditer(text):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                found.append(Shipment(carrier, m.group(1), status))
    return found
