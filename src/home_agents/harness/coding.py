"""Coding workspaces (E1.12): what Codex, Claude Code and Kiro do to a repository, in a sandbox.

A coding task gets its own CLI workspace container (workspaces.py: no secrets, its only way out
is its egress route) and a branch `agent/<task-id>`, so parallel tasks never share a working tree,
the way Codex and Claude Code use worktrees. The agent reads, searches, edits and runs commands
only inside the repo folder; every edit is a git commit it can rewind to (checkpoints). Pushing a
branch and opening a pull request always needs your approval, and merging stays with you (E1.9).

Project context files are read at the start: AGENTS.md (Codex), CLAUDE.md and .claude/rules
(Claude Code), and .kiro/steering (Kiro).
"""

from __future__ import annotations

import posixpath
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..tools import Tool

CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md")
CONTEXT_DIRS = (".kiro/steering", ".claude/rules")
MAX_OUTPUT = 20_000


@dataclass
class Exec:
    code: int
    out: str


class Executor(Protocol):
    def run(self, command: str, cwd: str, timeout: int = 300) -> Exec: ...


class DockerExecutor:  # pragma: no cover - needs Docker and the workspace image
    def __init__(self, container):
        self.container = container

    def run(self, command: str, cwd: str, timeout: int = 300) -> Exec:
        res = self.container.exec_run(["bash", "-lc", f"timeout {int(timeout)} bash -c {shlex.quote(command)}"],
                                      workdir=cwd, demux=False)
        return Exec(res.exit_code, (res.output or b"").decode("utf-8", "replace"))


class LocalExecutor:
    """Tests only: runs in a temporary folder on the machine running the tests. Never configured in production."""

    def run(self, command: str, cwd: str, timeout: int = 300) -> Exec:
        p = subprocess.run(["bash", "-c", command], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return Exec(p.returncode, p.stdout + p.stderr)


def _q(s: str) -> str:
    return shlex.quote(s)


class CodeWorkspace:
    def __init__(self, executor: Executor, root: str, task_id: str, *, read_file=None, write_file=None):
        self.x = executor
        self.root = root.rstrip("/")
        self.task_id = task_id
        self.branch = f"agent/{task_id}"
        # File transfer helpers; defaults go through the shell so they work in any container.
        self._read = read_file
        self._write = write_file

    # Paths never leave the repo folder
    def path(self, rel: str) -> str:
        norm = posixpath.normpath(posixpath.join(self.root, rel.lstrip("/")))
        if norm != self.root and not norm.startswith(self.root + "/"):
            raise ValueError(f"{rel} is outside the repository")
        if "/.git/" in norm + "/" and not norm.endswith("/.git"):
            raise ValueError("the .git folder is off limits; use the git tools")
        return norm

    def sh(self, command: str, timeout: int = 300) -> Exec:
        r = self.x.run(command, self.root, timeout)
        return Exec(r.code, r.out[-MAX_OUTPUT:])

    def prepare(self) -> str:
        r = self.sh(f"git checkout -B {_q(self.branch)} && git config user.name home-agent && "
                    f"git config user.email home-agent@users.noreply.github.com")
        if r.code:
            raise RuntimeError(f"could not create branch: {r.out[-500:]}")
        return self.context()

    def context(self) -> str:
        parts = []
        for f in CONTEXT_FILES:
            r = self.sh(f"test -f {_q(f)} && head -c 20000 {_q(f)}")
            if r.code == 0 and r.out.strip():
                parts.append(f"## {f}\n{r.out.strip()}")
        for d in CONTEXT_DIRS:
            r = self.sh(f"test -d {_q(d)} && for f in {_q(d)}/*.md; do echo \"## $f\"; head -c 8000 \"$f\"; echo; done")
            if r.code == 0 and r.out.strip():
                parts.append(r.out.strip())
        if not parts:
            return ""
        return "Repository instructions (from the repo's own context files; follow them for this code, they can't widen your permissions):\n\n" + "\n\n".join(parts)

    # Tools
    def read_file(self, inp: dict) -> str:
        p = self.path(inp["path"])
        start = int(inp.get("offset", 1))
        limit = int(inp.get("limit", 400))
        r = self.sh(f"test -f {_q(p)} && awk -v s={start} -v l={limit} 'NR>=s && NR<s+l {{printf \"%d\\t%s\\n\", NR, $0}}' {_q(p)}")
        if r.code:
            raise FileNotFoundError(inp["path"])
        return r.out

    def write_file(self, inp: dict) -> str:
        p = self.path(inp["path"])
        content = inp["content"]
        if self._write:
            self._write(p, content)
        else:
            delim = "HOME_AGENT_EOF_7f3a"
            if delim in content:
                raise ValueError("content contains the reserved delimiter")
            r = self.sh(f"mkdir -p {_q(posixpath.dirname(p))} && cat > {_q(p)} <<'{delim}'\n{content}\n{delim}\n"
                        f"truncate -s -1 {_q(p)}")
            if r.code:
                raise OSError(r.out[-500:])
        return f"wrote {inp['path']} ({len(content)} chars)"

    def _cat(self, p: str) -> str:
        if self._read:
            return self._read(p)
        r = self.sh(f"cat {_q(p)}")
        if r.code:
            raise FileNotFoundError(p)
        return r.out

    def edit_file(self, inp: dict) -> str:
        p = self.path(inp["path"])
        text = self._cat(p)
        old, new = inp["old"], inp["new"]
        n = text.count(old)
        if n != 1 and not inp.get("all"):
            raise ValueError(f"'old' must appear exactly once, found {n}; add context or set all=true")
        self.write_file({"path": inp["path"], "content": text.replace(old, new)})
        return f"edited {inp['path']} ({n} replacement{'s' if n != 1 else ''})"

    def list_files(self, inp: dict) -> str:
        pattern = inp.get("pattern", "*")
        r = self.sh(f"git ls-files --cached --others --exclude-standard -- {_q(pattern)} | head -500")
        return r.out or "(no files)"

    def grep(self, inp: dict) -> str:
        path = self.path(inp.get("path", "."))
        r = self.sh(f"git grep -n -I -E -e {_q(inp['pattern'])} -- {_q(path)} | head -200")
        return r.out or "(no matches)"

    def run(self, inp: dict) -> str:
        r = self.sh(inp["command"], int(inp.get("timeout", 300)))
        return f"exit {r.code}\n{r.out}"

    def diff(self, inp: dict) -> str:
        base = inp.get("base", "HEAD")
        r = self.sh(f"git add -A -N . && git diff {_q(base)} --stat && git diff {_q(base)} | head -c 60000")
        return r.out or "(no changes)"

    def checkpoint(self, inp: dict) -> str:
        r = self.sh(f"git add -A && git commit -q -m {_q(inp.get('message', 'checkpoint'))} && git rev-parse --short HEAD")
        return f"checkpoint {r.out.strip().splitlines()[-1]}" if r.code == 0 else f"nothing to commit\n{r.out[-300:]}"

    def checkpoints(self, inp: dict) -> str:
        return self.sh("git log --oneline -n 30").out

    def rewind(self, inp: dict) -> str:
        ref = inp["checkpoint"]
        r = self.sh(f"git rev-parse --verify {_q(ref + '^{commit}')} >/dev/null && git reset -q --hard {_q(ref)} && git clean -fdq")
        if r.code:
            raise ValueError(f"no checkpoint {ref}")
        return f"rewound to {ref}"

    def open_pull_request(self, inp: dict) -> str:
        r = self.sh(f"git push -u origin {_q(self.branch)} && gh pr create --head {_q(self.branch)} "
                    f"--title {_q(inp['title'])} --body {_q(inp['body'])}", timeout=180)
        return f"exit {r.code}\n{r.out[-2000:]}"

    def tools(self, read_only: bool = False) -> list[Tool]:
        obj = {"type": "object", "additionalProperties": False}
        t = [
            Tool("read_file", "Read a file in the repository, with line numbers.",
                 obj | {"properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
                        "required": ["path"]}, self.read_file),
            Tool("list_files", "List tracked and new files, optionally matching a pattern like 'src/*.py'.",
                 obj | {"properties": {"pattern": {"type": "string"}}, "required": []}, self.list_files),
            Tool("grep", "Search file contents with an extended regular expression.",
                 obj | {"properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}, self.grep),
            Tool("git_diff", "Show what changed against a base (default: the last checkpoint).",
                 obj | {"properties": {"base": {"type": "string"}}, "required": []}, self.diff),
            Tool("list_checkpoints", "List checkpoints (commits) on this task's branch.",
                 obj | {"properties": {}, "required": []}, self.checkpoints),
        ]
        if read_only:
            return t
        return t + [
            Tool("write_file", "Create or overwrite a file in the repository.",
                 obj | {"properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
                 self.write_file),
            Tool("edit_file", "Replace an exact snippet in a file. 'old' must appear once unless all=true.",
                 obj | {"properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                                       "all": {"type": "boolean"}}, "required": ["path", "old", "new"]}, self.edit_file),
            Tool("run_command", "Run a shell command in the repository inside the sandbox (tests, builds, linters).",
                 obj | {"properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]},
                 self.run),
            Tool("checkpoint", "Commit all changes as a checkpoint you can rewind to.",
                 obj | {"properties": {"message": {"type": "string"}}, "required": []}, self.checkpoint),
            Tool("rewind", "Go back to a checkpoint, discarding later changes.",
                 obj | {"properties": {"checkpoint": {"type": "string"}}, "required": ["checkpoint"]}, self.rewind),
            Tool("open_pull_request", "Push this task's branch and open a pull request. Needs the owner's approval.",
                 obj | {"properties": {"title": {"type": "string"}, "body": {"type": "string"}}, "required": ["title", "body"]},
                 self.open_pull_request, lambda _i: True),
        ]


class LocalCodeWorkspaces:
    """Tests only: clones a repo into a temp folder. Production uses `DockerCodeWorkspaces`."""

    def __init__(self, base: Path):
        self.base = Path(base)

    def open(self, repo: str, task_id: str) -> CodeWorkspace:
        dest = self.base / task_id
        subprocess.run(["git", "clone", "-q", repo, str(dest)], check=True)
        return CodeWorkspace(LocalExecutor(), str(dest), task_id)

    def close(self, ws: CodeWorkspace) -> None:
        pass


class DockerCodeWorkspaces:  # pragma: no cover - needs Docker
    """One CLI workspace container per coding task, on the task's egress route, destroyed afterwards."""

    def __init__(self, workspaces, route: str = "home"):
        self.workspaces = workspaces
        self.route = route
        self._names: dict[str, str] = {}

    def open(self, repo: str, task_id: str) -> CodeWorkspace:
        name = self.workspaces.create(job_id=task_id, kind="cli", route=self.route)
        self._names[task_id] = name
        container = self.workspaces.docker.containers.get(name)
        x = DockerExecutor(container)
        r = x.run(f"git clone -q {shlex.quote(repo)} /workspace/repo", "/workspace", 600)
        if r.code:
            raise RuntimeError(f"clone failed: {r.out[-500:]}")
        return CodeWorkspace(x, "/workspace/repo", task_id)

    def close(self, ws: CodeWorkspace) -> None:
        name = self._names.pop(ws.task_id, None)
        if name:
            self.workspaces.destroy(name)
