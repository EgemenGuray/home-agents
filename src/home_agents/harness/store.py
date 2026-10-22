"""Harness state in the shared SQLite file (E1.12): projects, chat sessions, tasks and artifacts."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '',
  profile TEXT NOT NULL DEFAULT 'assistant', created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, project_id TEXT, profile TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS session_messages (
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  model TEXT, channel TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS session_messages_sid ON session_messages(session_id, id);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL, profile TEXT NOT NULL,
  project_id TEXT, private INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, result TEXT,
  source TEXT NOT NULL DEFAULT 'app', attachments TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS task_events (
  id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS task_events_tid ON task_events(task_id, id);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, task_id TEXT, name TEXT NOT NULL, media_type TEXT NOT NULL, path TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

TASK_STATES = ("queued", "running", "waiting_approval", "done", "failed", "cancelled")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


class Store:
    def __init__(self, conn: sqlite3.Connection, data_dir: Path | str):
        self.conn = conn
        self.data_dir = Path(data_dir)
        conn.executescript(SCHEMA)

    # Projects: standing instructions and a default profile, like Cowork or ChatGPT projects
    def create_project(self, name: str, instructions: str = "", profile: str = "assistant") -> dict:
        pid = new_id("prj")
        self.conn.execute("INSERT INTO projects(id, name, instructions, profile) VALUES (?, ?, ?, ?)", (pid, name, instructions, profile))
        return self.project(pid)

    def project(self, pid: str | None) -> dict | None:
        if not pid:
            return None
        row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        return dict(row) if row else None

    def projects(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM projects ORDER BY created_at DESC")]

    def update_project(self, pid: str, **fields) -> dict | None:
        for k in ("name", "instructions", "profile"):
            if k in fields and fields[k] is not None:
                self.conn.execute(f"UPDATE projects SET {k} = ? WHERE id = ?", (fields[k], pid))
        return self.project(pid)

    # Chat sessions
    def create_session(self, profile: str, project_id: str | None = None, title: str = "") -> dict:
        sid = new_id("ses")
        self.conn.execute("INSERT INTO sessions(id, project_id, profile, title) VALUES (?, ?, ?, ?)", (sid, project_id, profile, title))
        return self.session(sid)

    def session(self, sid: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        return dict(row) if row else None

    def sessions(self, project_id: str | None = None) -> list[dict]:
        if project_id:
            rows = self.conn.execute("SELECT * FROM sessions WHERE project_id = ? ORDER BY updated_at DESC", (project_id,))
        else:
            rows = self.conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 100")
        return [dict(r) for r in rows]

    def add_message(self, sid: str, role: str, content: str, model: str | None = None, channel: str | None = None) -> None:
        self.conn.execute("INSERT INTO session_messages(session_id, role, content, model, channel) VALUES (?, ?, ?, ?, ?)",
                          (sid, role, content, model, channel))
        self.conn.execute("UPDATE sessions SET updated_at = datetime('now'), title = CASE WHEN title = '' THEN ? ELSE title END WHERE id = ?",
                          (content[:60] if role == "user" else "", sid))

    def messages(self, sid: str, limit: int = 40) -> list[dict]:
        rows = self.conn.execute("SELECT role, content, model, created_at FROM (SELECT * FROM session_messages WHERE session_id = ? "
                                 "ORDER BY id DESC LIMIT ?) ORDER BY id", (sid, limit))
        return [dict(r) for r in rows]

    # Tasks: background work with a visible event trail
    def create_task(self, title: str, prompt: str, profile: str, project_id: str | None = None, private: bool = False,
                    source: str = "app", attachments: list[dict] | None = None) -> dict:
        tid = new_id("task")
        self.conn.execute("INSERT INTO tasks(id, title, prompt, profile, project_id, private, state, source, attachments) "
                          "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                          (tid, title, prompt, profile, project_id, int(private), source, json.dumps(attachments or [])))
        return self.task(tid)

    def task(self, tid: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["attachments"] = json.loads(d["attachments"])
        d["private"] = bool(d["private"])
        return d

    def tasks(self, state: str | None = None, limit: int = 100) -> list[dict]:
        q, args = "SELECT id, title, profile, project_id, state, source, created_at, updated_at FROM tasks", []
        if state:
            q, args = q + " WHERE state = ?", [state]
        return [dict(r) for r in self.conn.execute(q + " ORDER BY created_at DESC, rowid DESC LIMIT ?", args + [limit])]

    def set_task_state(self, tid: str, state: str, result: str | None = None) -> None:
        assert state in TASK_STATES, state
        self.conn.execute("UPDATE tasks SET state = ?, result = COALESCE(?, result), updated_at = datetime('now') WHERE id = ?",
                          (state, result, tid))

    def add_event(self, tid: str, kind: str, data: dict) -> None:
        self.conn.execute("INSERT INTO task_events(task_id, kind, data) VALUES (?, ?, ?)", (tid, kind, json.dumps(data, default=str)))

    def events(self, tid: str, after: int = 0) -> list[dict]:
        rows = self.conn.execute("SELECT id, kind, data, created_at FROM task_events WHERE task_id = ? AND id > ? ORDER BY id", (tid, after))
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    # Artifacts: files an agent produced, shown in the console and the app
    def save_artifact(self, name: str, content: bytes, media_type: str, task_id: str | None = None) -> dict:
        aid = new_id("art")
        folder = self.data_dir / "artifacts" / (task_id or "loose")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{aid}-{_SAFE_NAME.sub('_', name)[:80]}"
        path.write_bytes(content)
        self.conn.execute("INSERT INTO artifacts(id, task_id, name, media_type, path) VALUES (?, ?, ?, ?, ?)",
                          (aid, task_id, name, media_type, str(path)))
        return {"id": aid, "name": name, "media_type": media_type}

    def artifacts(self, task_id: str | None = None) -> list[dict]:
        if task_id:
            rows = self.conn.execute("SELECT id, task_id, name, media_type, created_at FROM artifacts WHERE task_id = ? ORDER BY created_at", (task_id,))
        else:
            rows = self.conn.execute("SELECT id, task_id, name, media_type, created_at FROM artifacts ORDER BY created_at DESC LIMIT 200")
        return [dict(r) for r in rows]

    def artifact(self, aid: str) -> tuple[dict, bytes] | None:
        row = self.conn.execute("SELECT * FROM artifacts WHERE id = ?", (aid,)).fetchone()
        if not row:
            return None
        return dict(row), Path(row["path"]).read_bytes()
