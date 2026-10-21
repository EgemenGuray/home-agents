"""Local model on the Mac Studio (S2.3): private requests and the offline fallback.

The Studio serves an open-weight model with llama.cpp's server or mlx_lm.server, both of which
expose a chat-completions HTTP endpoint. Answers are text only; device control still goes
through HA's local intents, which never needed the cloud.
"""

from __future__ import annotations

import httpx

LOCAL_SYSTEM = ("You are the home's offline assistant. Answer briefly for speech. You cannot browse the web; "
                "say so if a question needs current information.")


class LocalModel:
    def __init__(self, url: str, model: str = "local", timeout: float = 60.0, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.model = model
        self.http = client or httpx.Client(timeout=timeout)

    def available(self) -> bool:
        try:
            return self.http.get(f"{self.url}/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def answer(self, history: list[dict], text: str, facts: str = "") -> tuple[str, int, int]:
        messages = [{"role": "system", "content": f"{LOCAL_SYSTEM}\nHousehold facts:\n{facts}"}]
        messages += [m for m in history if isinstance(m.get("content"), str)]
        messages.append({"role": "user", "content": text})
        r = self.http.post(f"{self.url}/v1/chat/completions",
                           json={"model": self.model, "messages": messages, "max_tokens": 600, "temperature": 0.3})
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage", {})
        return data["choices"][0]["message"]["content"].strip(), usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
