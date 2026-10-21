"""Agent-to-agent messaging policy (S1.5.2, S1.5.3).

Only pairs on the allow-list may talk. Everything else is blocked and logged.
Message content never changes the policy: it is data, not instructions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

WILDCARD = "*"


@dataclass(frozen=True)
class MessagePolicy:
    allowed_pairs: frozenset[frozenset[str]] = field(default_factory=frozenset)

    @classmethod
    def from_config(cls, cfg: dict) -> "MessagePolicy":
        pairs = cfg.get("messaging", {}).get("allow", [])
        for pair in pairs:
            if len(pair) != 2:
                raise ValueError(f"allow entry must be a pair: {pair!r}")
        return cls(frozenset(frozenset(p) for p in pairs))

    def allows(self, sender: str, recipient: str) -> bool:
        if sender == recipient:
            return False
        for pair in self.allowed_pairs:
            members = set(pair)
            if WILDCARD in members:
                other = members - {WILDCARD}
                # ["*", "*"] collapses to {"*"}: never allow everyone to talk to everyone.
                if other and (sender in other or recipient in other):
                    return True
            elif members == {sender, recipient}:
                return True
        return False

    def check(self, sender: str, recipient: str) -> bool:
        ok = self.allows(sender, recipient)
        if not ok:
            log.warning("blocked agent message %s -> %s", sender, recipient)
        return ok
