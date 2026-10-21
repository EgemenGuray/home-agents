"""Shared memory: household facts and conversation history (S1.1.3, S0.5.10)."""

from __future__ import annotations

import sqlite3


class Memory:
    def __init__(self, conn: sqlite3.Connection, history_turns: int = 12):
        self.conn = conn
        self.history_turns = history_turns

    # Facts the agent may read: "what do you know about me?", "forget that"
    def remember(self, subject: str, fact: str, source: str = "user") -> int:
        cur = self.conn.execute("INSERT INTO facts(subject, fact, source) VALUES (?, ?, ?)", (subject, fact, source))
        return int(cur.lastrowid)

    def facts(self, subject: str | None = None) -> list[dict]:
        if subject:
            rows = self.conn.execute("SELECT * FROM facts WHERE subject = ? ORDER BY id", (subject,))
        else:
            rows = self.conn.execute("SELECT * FROM facts ORDER BY id")
        return [dict(r) for r in rows]

    def forget(self, fact_id: int) -> bool:
        return self.conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,)).rowcount > 0

    def forget_matching(self, text: str) -> int:
        return self.conn.execute("DELETE FROM facts WHERE fact LIKE ?", (f"%{text}%",)).rowcount

    def facts_prompt(self) -> str:
        rows = self.facts()
        if not rows:
            return "No stored household facts yet."
        return "\n".join(f"- ({r['subject']}) {r['fact']}" for r in rows)

    # Conversation history, shared across voice, app and Telegram by conversation id
    def add_turn(self, conversation_id: str, role: str, content: str, channel: str | None = None, room: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO turns(conversation_id, role, content, channel, room) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, channel, room),
        )

    def history(self, conversation_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content FROM (SELECT * FROM turns WHERE conversation_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (conversation_id, self.history_turns),
        )
        msgs = [{"role": r["role"], "content": r["content"]} for r in rows]
        while msgs and msgs[0]["role"] != "user":  # the API needs a user turn first
            msgs.pop(0)
        return msgs
