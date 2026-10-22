"""Model providers for the harness (E1.12): Claude, any OpenAI-compatible API, and local servers.

One neutral message format goes in, one ChatResult comes out, so the tool loop, approvals and
audit never depend on a vendor. "openai" covers OpenAI itself, Gemini's OpenAI endpoint,
OpenRouter, and the local servers on the Studio: Ollama, llama.cpp, mlx_lm.server, LM Studio, vLLM.

Neutral messages:
  {"role": "user", "content": "text", "images": [{"media_type": "image/png", "data": "<base64>"}]}
  {"role": "assistant", "content": "text", "tool_calls": [{"id", "name", "input"}]}
  {"role": "tool", "tool_call_id": "...", "content": "text", "is_error": False}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop: str = "end"            # end | tool_use | refusal | max_tokens
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""              # provider:model, as spend records it

    def as_message(self) -> dict:
        return {"role": "assistant", "content": self.text,
                "tool_calls": [{"id": c.id, "name": c.name, "input": c.input} for c in self.tool_calls]}


class ProviderDown(Exception):
    """Connection failures, timeouts and 5xx: the router moves on to the next model."""


class Provider(Protocol):
    name: str
    location: str  # "cloud" or "local"

    def available(self) -> bool: ...

    def chat(self, model: str, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> ChatResult: ...


class AnthropicProvider:
    """Claude through the official SDK; the key is read by the SDK from ANTHROPIC_API_KEY."""

    def __init__(self, name: str = "claude", client=None, location: str = "cloud"):
        self.name = name
        self.location = location
        self._client = client

    @property
    def client(self):
        if self._client is None:  # pragma: no cover
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def available(self) -> bool:
        return self._client is not None or bool(os.environ.get("ANTHROPIC_API_KEY"))

    def chat(self, model, system, messages, tools, max_tokens) -> ChatResult:
        import anthropic

        try:
            r = self.client.messages.create(
                model=model, max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=to_anthropic(messages),
                tools=[{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools],
            )
        except (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.InternalServerError) as exc:
            raise ProviderDown(str(exc)) from exc
        texts = [b.text for b in r.content if getattr(b, "type", None) == "text"]
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in r.content if getattr(b, "type", None) == "tool_use"]
        stop = {"tool_use": "tool_use", "refusal": "refusal", "max_tokens": "max_tokens"}.get(r.stop_reason, "end")
        return ChatResult(" ".join(t.strip() for t in texts if t.strip()), calls, stop,
                          r.usage.input_tokens, r.usage.output_tokens, f"{self.name}:{model}")


class OpenAICompatProvider:
    """Chat Completions with function calling. Works for cloud APIs and every local server we use."""

    def __init__(self, name: str, base_url: str, api_key_env: str | None = None, location: str = "cloud",
                 timeout: float = 120.0, client: httpx.Client | None = None, health_path: str = "/models"):
        self.name = name
        self.location = location
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.health_path = health_path
        self.http = client or httpx.Client(timeout=timeout)

    def _headers(self) -> dict:
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        return {"Authorization": f"Bearer {key}"} if key else {}

    def available(self) -> bool:
        if self.api_key_env and not os.environ.get(self.api_key_env):
            return False
        if self.location == "cloud":
            return True  # don't spend a request on a health check; failures fall through to the next model
        try:
            return self.http.get(f"{self.base_url}{self.health_path}", headers=self._headers(), timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def chat(self, model, system, messages, tools, max_tokens) -> ChatResult:
        body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "system", "content": system}] + to_openai(messages)}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                "parameters": t["input_schema"]}} for t in tools]
        try:
            r = self.http.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderDown(str(exc)) from exc
        if r.status_code >= 500 or r.status_code == 429:
            raise ProviderDown(f"{self.name} returned {r.status_code}")
        r.raise_for_status()
        data = r.json()
        choice = data["choices"][0]
        msg = choice.get("message", {})
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparsed": fn.get("arguments", "")}
            calls.append(ToolCall(c.get("id") or f"call_{len(calls)}", fn.get("name", ""), args if isinstance(args, dict) else {}))
        finish = choice.get("finish_reason")
        stop = "tool_use" if calls else {"length": "max_tokens", "content_filter": "refusal"}.get(finish, "end")
        usage = data.get("usage") or {}
        return ChatResult((msg.get("content") or "").strip(), calls, stop,
                          usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), f"{self.name}:{model}")


def to_anthropic(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if m.get("is_error"):
                block["is_error"] = True
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)  # results of one turn share one user message
            else:
                out.append({"role": "user", "content": [block]})
        elif m["role"] == "assistant":
            content = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            content += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]} for c in m.get("tool_calls") or []]
            out.append({"role": "assistant", "content": content or m.get("content", "")})
        else:
            images = m.get("images") or []
            if images:
                content = [{"type": "image", "source": {"type": "base64", "media_type": i["media_type"], "data": i["data"]}} for i in images]
                out.append({"role": "user", "content": content + [{"type": "text", "text": m["content"]}]})
            else:
                out.append({"role": "user", "content": m["content"]})
    return out


def to_openai(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        elif m["role"] == "assistant":
            msg: dict = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": json.dumps(c["input"])}} for c in m["tool_calls"]]
            out.append(msg)
        else:
            images = m.get("images") or []
            if images:
                parts = [{"type": "image_url", "image_url": {"url": f"data:{i['media_type']};base64,{i['data']}"}} for i in images]
                out.append({"role": "user", "content": parts + [{"type": "text", "text": m["content"]}]})
            else:
                out.append({"role": "user", "content": m["content"]})
    return out


def build_providers(config: dict, anthropic_client=None) -> dict[str, Provider]:
    """From the `providers:` block of config/harness.yaml. Keys are env var names, never values."""
    out: dict[str, Provider] = {}
    for name, p in (config or {}).items():
        kind = p.get("type")
        if kind == "anthropic":
            out[name] = AnthropicProvider(name, anthropic_client, p.get("location", "cloud"))
        elif kind == "openai":
            out[name] = OpenAICompatProvider(name, p["base_url"], p.get("api_key_env"), p.get("location", "cloud"),
                                             health_path=p.get("health_path", "/models"))
        else:
            raise ValueError(f"provider {name}: unknown type {kind!r}")
    return out
