"""Checking what an agent hands back (E1.12): CrewAI's guardrails and structured output.

A task or crew step can say what a good result looks like:

    expected_output: "A table of three options with prices in CAD"
    output_schema: {type: object, required: [options], properties: {options: {type: array}}}
    guardrail: {max_words: 300, must_include: ["CAD"], must_not_include: ["as an AI"]}

If a result fails, the agent gets the reasons and another try (two by default).
The schema check covers the JSON Schema subset these configs use: type, required,
properties, items, enum, minItems, maxItems.
"""

from __future__ import annotations

import json
import re

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}


def schema_errors(value, schema: dict, path: str = "$") -> list[str]:
    errs: list[str] = []
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(isinstance(value, _TYPES[x]) and not (x in ("integer", "number") and isinstance(value, bool)) for x in types):
            return [f"{path} should be {t}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path} should be one of {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errs.append(f"{path}.{key} is missing")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                errs += schema_errors(value[key], sub, f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errs.append(f"{path} needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path} allows at most {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                errs += schema_errors(item, schema["items"], f"{path}[{i}]")
    return errs


def extract_json(text: str):
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found in the result")


def check(text: str, schema: dict | None = None, guardrail: dict | None = None) -> tuple[list[str], object]:
    """Returns (problems, parsed). parsed is the JSON value when a schema is given, else the text."""
    problems: list[str] = []
    parsed: object = text
    if schema:
        try:
            parsed = extract_json(text)
            problems += schema_errors(parsed, schema)
        except ValueError as exc:
            problems.append(str(exc))
    g = guardrail or {}
    words = len(text.split())
    if "max_words" in g and words > g["max_words"]:
        problems.append(f"{words} words, the limit is {g['max_words']}")
    if "min_words" in g and words < g["min_words"]:
        problems.append(f"{words} words, at least {g['min_words']} needed")
    for s in g.get("must_include", []):
        if s.lower() not in text.lower():
            problems.append(f"must mention {s!r}")
    for s in g.get("must_not_include", []):
        if s.lower() in text.lower():
            problems.append(f"must not contain {s!r}")
    if "regex" in g and not re.search(g["regex"], text, re.S):
        problems.append(f"must match {g['regex']!r}")
    return problems, parsed


def instructions(expected_output: str = "", schema: dict | None = None) -> str:
    parts = []
    if expected_output:
        parts.append(f"Expected output: {expected_output}")
    if schema:
        parts.append(f"End with a JSON value matching this schema, in a ```json block:\n{json.dumps(schema)}")
    return "\n".join(parts)
