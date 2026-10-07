"""Minimal Ollama client (standard library only).

Ollama runs the model locally — on the desktop GPU in Phase 1, on the Pi 5
in Phase 2 — so there is no API key and no monthly bill.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


class Ollama:
    def __init__(self, url: str, model: str, timeout: float = 300):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise LLMError(f"Ollama returned {e.code}: {e.read().decode(errors='replace')}") from e
        except urllib.error.URLError as e:
            raise LLMError(
                f"Can't reach Ollama at {self.url} ({e.reason}). Is it running? Try `ollama serve`."
            ) from e

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Return the assistant message dict (``content`` and maybe ``tool_calls``)."""
        payload = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        return self._post("/api/chat", payload)["message"]

    def available(self) -> bool:
        try:
            req = urllib.request.Request(self.url + "/api/tags")
            with urllib.request.urlopen(req, timeout=3) as resp:
                names = [m["name"] for m in json.loads(resp.read()).get("models", [])]
            return any(n == self.model or n.split(":")[0] == self.model for n in names)
        except (urllib.error.URLError, OSError, ValueError):
            return False
