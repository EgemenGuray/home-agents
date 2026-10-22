"""Chat channels (E1.12): talk to the harness from chat apps, like OpenClaw's gateway.

Every channel polls outward, so nothing listens on the internet (zero inbound ports). Only
paired senders get through: an unknown sender gets a pairing code, and you approve it from the
console or the app. Each sender has their own chat session.
"""

from __future__ import annotations

import secrets
from typing import Callable, Protocol

import httpx


class Channel(Protocol):
    name: str

    def poll(self) -> list[tuple[str, str]]:
        """New (sender_id, text) messages since the last poll."""

    def send(self, sender_id: str, text: str) -> None: ...


class Pairing:
    def __init__(self, allowed: set[str] | None = None):
        self.allowed = set(allowed or ())
        self.codes: dict[str, str] = {}  # code -> "channel:sender"

    def is_allowed(self, key: str) -> bool:
        return key in self.allowed

    def code_for(self, key: str) -> str:
        for code, k in self.codes.items():
            if k == key:
                return code
        code = f"{secrets.randbelow(10**6):06d}"
        self.codes[code] = key
        return code

    def approve(self, code: str) -> str | None:
        key = self.codes.pop(code, None)
        if key:
            self.allowed.add(key)
        return key


class Gateway:
    """Moves messages between channels and the harness chat. `chat(session_key, text)` returns the reply."""

    def __init__(self, channels: list[Channel], chat: Callable[[str, str, str], str], pairing: Pairing, audit=None):
        self.channels = {c.name: c for c in channels}
        self.chat = chat
        self.pairing = pairing
        self.audit = audit

    def poll_once(self) -> int:
        handled = 0
        for ch in self.channels.values():
            for sender, text in ch.poll():
                key = f"{ch.name}:{sender}"
                if not self.pairing.is_allowed(key):
                    code = self.pairing.code_for(key)
                    if self.audit:
                        self.audit.record("channel_pairing_requested", "gateway", {"key": key})
                    ch.send(sender, f"This assistant is private. Pairing code: {code}. The owner must approve it.")
                    continue
                ch.send(sender, self.chat(key, text, ch.name))
                handled += 1
        return handled


class TelegramChannel:
    """Long polling with getUpdates, so the bot needs no webhook and no open port."""

    name = "telegram"

    def __init__(self, token: str, client: httpx.Client | None = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = client or httpx.Client(timeout=40)
        self.offset = 0

    def poll(self) -> list[tuple[str, str]]:
        r = self.http.get(f"{self.base}/getUpdates", params={"timeout": 25, "offset": self.offset})
        r.raise_for_status()
        out = []
        for u in r.json().get("result", []):
            self.offset = max(self.offset, u["update_id"] + 1)
            msg = u.get("message") or {}
            if msg.get("chat", {}).get("type") == "private" and msg.get("text"):
                out.append((str(msg["chat"]["id"]), msg["text"]))
        return out

    def send(self, sender_id: str, text: str) -> None:
        self.http.post(f"{self.base}/sendMessage", json={"chat_id": sender_id, "text": text[:4000]}).raise_for_status()
