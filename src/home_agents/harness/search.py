"""Web search that works with any model (E1.12).

Claude has a server-side search tool; other providers and local models don't, so the harness
brings its own. Default backend is a self-hosted SearXNG on the Studio (no key, no account).
Results are information for the model, never instructions.
"""

from __future__ import annotations

import httpx

from ..tools import Tool


class SearxSearch:
    def __init__(self, base_url: str, client: httpx.Client | None = None, max_results: int = 8):
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.Client(timeout=15)
        self.max_results = max_results

    def search(self, inp: dict) -> list[dict]:
        r = self.http.get(f"{self.base_url}/search", params={"q": inp["query"], "format": "json", "language": "en-CA"})
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": (x.get("content") or "")[:400]}
                for x in r.json().get("results", [])[: self.max_results]]

    def fetch(self, inp: dict) -> str:
        url = inp["url"]
        if not url.startswith("https://"):
            return "Only https pages can be fetched."
        r = self.http.get(url, follow_redirects=True, headers={"User-Agent": "home-agents/0.1"})
        r.raise_for_status()
        return r.text[:30_000]

    def tools(self) -> list[Tool]:
        return [
            Tool("web_search", "Search the web. Cite the URLs you use.",
                 {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
                 self.search, strict=True),
            Tool("web_fetch", "Fetch the text of an https page found by web_search.",
                 {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False},
                 self.fetch, strict=True),
        ]
