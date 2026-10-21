"""Audit log of tool calls, messages, approvals, logins and takeovers (S1.8.1).

Values that look like secrets are redacted before they are written. Kept 30 days.
"""

from __future__ import annotations

import json
import re
import sqlite3

_REDACT = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"tskey-[A-Za-z0-9\-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # card numbers
]
_SECRET_KEYS = {"password", "passwd", "secret", "token", "otp", "code", "ciphertext", "api_key", "cvv"}


def redact(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if k.lower() in _SECRET_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern in _REDACT:
            value = pattern.sub("[redacted]", value)
    return value


class AuditLog:
    def __init__(self, conn: sqlite3.Connection, retention_days: int = 30):
        self.conn = conn
        self.retention_days = retention_days

    def record(self, kind: str, actor: str, detail: dict, job_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit(kind, actor, detail, job_id) VALUES (?, ?, ?, ?)",
            (kind, actor, json.dumps(redact(detail), default=str), job_id),
        )

    def search(self, text: str = "", kind: str | None = None, limit: int = 100) -> list[dict]:
        q = "SELECT * FROM audit WHERE detail LIKE ?"
        args: list = [f"%{text}%"]
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(q, args)]

    def purge(self) -> int:
        cur = self.conn.execute(
            "DELETE FROM audit WHERE created_at < datetime('now', ?)", (f"-{self.retention_days} days",)
        )
        return cur.rowcount
