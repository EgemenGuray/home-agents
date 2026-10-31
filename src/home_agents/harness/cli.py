"""`home-agent`: the harness from a terminal (E1.12), like the Codex and Claude Code CLIs.

Talks to the agent service over the tailnet with the service token, so it works from any of
your devices and never needs the model keys. Set HOME_AGENTS_URL (default http://127.0.0.1:8700)
and HOME_AGENTS_TOKEN.

    home-agent chat "what's on the calendar tomorrow?"
    home-agent task "tidy the README" --mode plan --repo https://github.com/you/home-agents
    home-agent watch <task-id>            live steps until the task ends
    home-agent answer <question-id> "the blue one"
    home-agent halt                        emergency stop for every task
    home-agent recall "dentist"            search long-term memory
    home-agent sleep                       run memory consolidation now
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def client() -> httpx.Client:
    token = os.environ.get("HOME_AGENTS_TOKEN", "")
    if not token:
        sys.exit("Set HOME_AGENTS_TOKEN to the agent service token.")
    return httpx.Client(base_url=os.environ.get("HOME_AGENTS_URL", "http://127.0.0.1:8700"),
                        headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(30, read=None))


def _print(data) -> None:
    print(json.dumps(data, indent=2, default=str))


def _check(r: httpx.Response):
    if r.status_code >= 400:
        sys.exit(f"{r.status_code}: {r.text[:500]}")
    return r.json()


def watch(c: httpx.Client, tid: str, out=None) -> str:
    """Follow a task's event stream; returns the final state."""
    out = out or sys.stdout
    state = ""
    with c.stream("GET", f"/v1/h/tasks/{tid}/stream") as r:
        event = ""
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if event == "end":
                    state = data.get("state", "")
                else:
                    detail = data.get("data", {})
                    print(f"[{event}] {json.dumps(detail, default=str)[:300]}", file=out)
    return state


def main(argv: list[str] | None = None, c: httpx.Client | None = None) -> int:
    p = argparse.ArgumentParser(prog="home-agent", description="Talk to the home agent harness.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("chat"); s.add_argument("text"); s.add_argument("--session"); s.add_argument("--profile")
    s.add_argument("--model"); s.add_argument("--private", action="store_true"); s.add_argument("--temporary", action="store_true")
    s = sub.add_parser("task"); s.add_argument("prompt"); s.add_argument("--title", default=""); s.add_argument("--profile", default="assistant")
    s.add_argument("--mode", default="auto", choices=["auto", "plan", "spec", "crew", "flow"]); s.add_argument("--repo")
    s.add_argument("--model"); s.add_argument("--crew"); s.add_argument("--flow"); s.add_argument("--private", action="store_true")
    s.add_argument("--watch", action="store_true")
    s = sub.add_parser("tasks"); s.add_argument("--state")
    for name in ("watch", "pause", "resume", "stop", "show"):
        sub.add_parser(name).add_argument("task_id")
    s = sub.add_parser("steer"); s.add_argument("task_id"); s.add_argument("text")
    sub.add_parser("questions")
    s = sub.add_parser("answer"); s.add_argument("question_id"); s.add_argument("text")
    sub.add_parser("halt"); sub.add_parser("resume-all"); sub.add_parser("metrics")
    s = sub.add_parser("recall"); s.add_argument("query"); s.add_argument("-k", type=int, default=10)
    s = sub.add_parser("forget"); s.add_argument("memory_id", type=int)
    s = sub.add_parser("sleep"); s.add_argument("--allow-cloud", action="store_true")
    a = p.parse_args(argv)
    c = c or client()

    if a.cmd == "chat":
        d = _check(c.post("/v1/h/chat", json={"text": a.text, "session_id": a.session, "profile": a.profile, "model": a.model,
                                              "private": a.private or None, "temporary": a.temporary}))
        print(d["text"])
        if d.get("pending_approvals"):
            print("(waiting for your approval on the phone)")
        if d.get("session"):
            print(f"\nsession: {d['session']['id']}", file=sys.stderr)
    elif a.cmd == "task":
        options = {k: v for k, v in {"repo": a.repo, "model": a.model, "crew": a.crew, "flow": a.flow}.items() if v}
        t = _check(c.post("/v1/h/tasks", json={"prompt": a.prompt, "title": a.title, "profile": a.profile, "mode": a.mode,
                                               "options": options, "private": a.private}))
        print(t["id"])
        if a.watch:
            print(watch(c, t["id"]))
    elif a.cmd == "tasks":
        for t in _check(c.get("/v1/h/tasks", params={"state": a.state} if a.state else None)):
            print(f"{t['id']}  {t['state']:<16} {t['title']}")
    elif a.cmd == "show":
        _print(_check(c.get(f"/v1/h/tasks/{a.task_id}")))
    elif a.cmd == "watch":
        print(watch(c, a.task_id))
    elif a.cmd in ("pause", "resume"):
        _print(_check(c.post(f"/v1/h/tasks/{a.task_id}/{a.cmd}")))
    elif a.cmd == "stop":
        _print(_check(c.post(f"/v1/h/tasks/{a.task_id}/cancel")))
    elif a.cmd == "steer":
        _print(_check(c.post(f"/v1/h/tasks/{a.task_id}/steer", json={"text": a.text})))
    elif a.cmd == "questions":
        for q in _check(c.get("/v1/h/questions")):
            print(f"{q['id']}  task {q['task_id']}: {q['question']}" + (f"  [{' / '.join(q['choices'])}]" if q["choices"] else ""))
    elif a.cmd == "answer":
        _print(_check(c.post(f"/v1/h/questions/{a.question_id}", json={"text": a.text})))
    elif a.cmd == "halt":
        _print(_check(c.post("/v1/h/halt")))
    elif a.cmd == "resume-all":
        _print(_check(c.post("/v1/h/resume-all")))
    elif a.cmd == "metrics":
        _print(_check(c.get("/v1/h/metrics")))
    elif a.cmd == "recall":
        for m in _check(c.get("/v1/h/memory", params={"q": a.query, "k": a.k})):
            print(f"#{m['id']} [{m['kind']}, {m['created_at'][:10]}, strength {m['strength']:.2f}] {m['content'][:200]}")
    elif a.cmd == "forget":
        _print(_check(c.delete(f"/v1/h/memory/{a.memory_id}")))
    elif a.cmd == "sleep":
        _print(_check(c.post("/v1/h/memory/sleep", params={"allow_cloud": str(a.allow_cloud).lower()})))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
