"""MCP servers as tools (E1.12), the way Cowork and ChatGPT use connectors.

Each server in harness.yaml becomes a set of tools named `<server>__<tool>`. Write-type tools
default to "ask" unless the config says otherwise, and the profile stances (allow, ask, block)
apply on top. Servers run locally over stdio or on the tailnet over HTTP; their tokens come
from environment variables named in the config, never from the file itself.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import threading
from typing import Protocol

from ..tools import Tool

READ_ONLY_HINTS = ("get", "list", "search", "read", "find", "fetch", "query", "describe")


class McpClient(Protocol):
    def list_tools(self) -> list[dict]: ...

    def call(self, name: str, arguments: dict) -> str: ...


def looks_read_only(name: str) -> bool:
    return name.lower().split("_")[0] in READ_ONLY_HINTS


def wrap_server(server: str, client: McpClient, ask: list[str] | None = None, allow: list[str] | None = None) -> list[Tool]:
    """`ask` and `allow` are glob lists for this server's tool names; anything else asks unless it looks read-only."""
    tools = []
    for t in client.list_tools():
        name = t["name"]
        always_ask = any(fnmatch.fnmatch(name, p) for p in ask or [])
        allowed = any(fnmatch.fnmatch(name, p) for p in allow or [])
        needs = always_ask or not (allowed or looks_read_only(name))
        tools.append(Tool(f"{server}__{name}", f"[{server}] {t.get('description', '')}"[:1000],
                          t.get("input_schema") or {"type": "object", "properties": {}},
                          lambda inp, _n=name: client.call(_n, inp),
                          (lambda _inp: True) if needs else (lambda _inp: False)))
    return tools


class StdioMcpClient:  # pragma: no cover - needs a real server process
    """Runs the MCP SDK's async client on its own event loop thread and exposes sync calls."""

    def __init__(self, command: str, args: list[str], env_names: list[str] | None = None):
        from mcp import StdioServerParameters

        env = {k: os.environ[k] for k in (env_names or []) if k in os.environ}
        env["PATH"] = os.environ.get("PATH", "")
        self.params = StdioServerParameters(command=command, args=args, env=env)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.session = None
        self._run(self._connect())

    def _run(self, coro, timeout: float = 120):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _connect(self):
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(self.params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

    def list_tools(self) -> list[dict]:
        res = self._run(self.session.list_tools())
        return [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema} for t in res.tools]

    def call(self, name: str, arguments: dict) -> str:
        res = self._run(self.session.call_tool(name, arguments))
        texts = [getattr(c, "text", "") for c in res.content]
        out = "\n".join(t for t in texts if t) or "(no text result)"
        if getattr(res, "isError", False):
            raise RuntimeError(out[:500])
        return out


def load_mcp_tools(config: dict, factory=None) -> list[Tool]:  # pragma: no cover - thin config glue
    factory = factory or (lambda c: StdioMcpClient(c["command"], c.get("args", []), c.get("env", [])))
    tools: list[Tool] = []
    for server, c in (config or {}).items():
        tools += wrap_server(server, factory(c), c.get("ask"), c.get("allow"))
    return tools
