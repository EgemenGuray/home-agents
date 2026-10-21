"""Agents' Gmail access skips security mail (S1.8.3).

Password resets, 2FA codes, new-sign-in and recovery emails are hidden from agents unless
you approve that one message. Applied to every message before an agent can read it.
"""

from __future__ import annotations

import re

_SUBJECT = re.compile(
    r"(verification code|security code|one[- ]time (pass)?code|\b2fa\b|two[- ]factor|sign[- ]in attempt|new sign[- ]in|"
    r"password (reset|change)|reset your password|recovery (email|phone)|security alert|confirm your (email|identity)|"
    r"login code|passcode|\botp\b)", re.I)
_SENDERS = re.compile(r"(no-reply@accounts\.google\.com|account-security|security@|verify@|appleid@id\.apple\.com|"
                      r"account-update@amazon)", re.I)
_CODE = re.compile(r"\b\d{6}\b")


def is_security_mail(sender: str, subject: str, snippet: str = "") -> bool:
    if _SUBJECT.search(subject) or _SENDERS.search(sender):
        return True
    return bool(_CODE.search(snippet) and re.search(r"code|verify|sign", snippet, re.I))


def filter_for_agent(messages: list[dict], approved_ids: set[str] = frozenset()) -> list[dict]:
    """messages: dicts with id, from, subject, snippet (Gmail API metadata)."""
    out = []
    for m in messages:
        if m["id"] in approved_ids or not is_security_mail(m.get("from", ""), m.get("subject", ""), m.get("snippet", "")):
            out.append(m)
    return out
