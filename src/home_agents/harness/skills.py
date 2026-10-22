"""Skills (E1.12, E3.5): reusable procedures the agent loads by name when it needs them.

Reads the Agent Skills layout (`skills/<name>/SKILL.md` with `name` and `description` in YAML
front matter), which is also what OpenClaw's skills use, plus our older flat `skills/<name>.md`.
Only names and descriptions go into the prompt; the body is loaded on demand. Skills are code:
a skill from anywhere else is reviewed and added by pull request, never installed at runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..tools import Tool

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path


def parse_skill(path: Path) -> Skill:
    text = path.read_text()
    meta: dict = {}
    m = _FRONT.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}  # a broken header still leaves a usable skill, named after its folder
        text = text[m.end():]
    default_name = path.parent.name if path.name == "SKILL.md" else path.stem
    description = meta.get("description") or next(
        (ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")), "")
    return Skill(str(meta.get("name") or default_name), str(description), text.strip(), path)


class SkillLibrary:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self._skills: dict[str, Skill] = {}
        self.reload()

    def reload(self) -> None:
        found: dict[str, Skill] = {}
        if self.root.is_dir():
            paths = sorted(self.root.glob("*/SKILL.md")) + sorted(p for p in self.root.glob("*.md") if p.name != "README.md")
            for p in paths:
                s = parse_skill(p)
                found.setdefault(s.name, s)
        self._skills = found

    def names(self) -> list[str]:
        return sorted(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def allowed(self, patterns: list[str] | None) -> list[Skill]:
        patterns = patterns if patterns is not None else ["*"]
        return [s for n, s in sorted(self._skills.items()) if "*" in patterns or n in patterns]

    def prompt(self, patterns: list[str] | None = None) -> str:
        skills = self.allowed(patterns)
        if not skills:
            return ""
        lines = "\n".join(f"- {s.name}: {s.description}" for s in skills)
        return f"Skills you can load with load_skill when a task matches one:\n{lines}"

    def tool(self, patterns: list[str] | None = None) -> Tool:
        allowed = {s.name for s in self.allowed(patterns)}

        def load(inp: dict) -> str:
            name = inp.get("name", "")
            if name not in allowed:
                return f"No skill named {name!r}. Available: {', '.join(sorted(allowed)) or 'none'}."
            return self._skills[name].body

        return Tool("load_skill", "Load the full steps of a skill by name before following it.",
                    {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
                     "additionalProperties": False}, load, strict=True)
