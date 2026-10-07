"""Thin client for a local Ollama server (https://ollama.com)."""

from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

from .config import settings


class LLMError(RuntimeError):
    pass


class Ollama:
    def __init__(self, base_url: str | None = None, timeout: float = 300.0):
        self.base_url = (base_url or settings.ollama_url).rstrip("/")
        self.timeout = timeout

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
        fmt: str | None = None,
    ) -> dict[str, Any]:
        """Non-streaming chat. Returns Ollama's `message` dict (role, content, tool_calls?)."""
        payload: dict[str, Any] = {
            "model": model or settings.chat_model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options
        if fmt:
            payload["format"] = fmt
        try:
            r = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise LLMError(f"Can't reach Ollama at {self.base_url} — is the Ollama app running? ({e})") from e
        if r.status_code == 404:
            raise LLMError(
                f"The AI model '{payload['model']}' isn't downloaded yet. "
                f"In Terminal run:  ollama pull {payload['model']}   (or ./scripts/pull-models.sh)"
            )
        if r.is_error:
            raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return r.json().get("message", {"role": "assistant", "content": ""})

    def stream_chat(
        self, messages: list[dict[str, Any]], model: str | None = None
    ) -> Iterator[str]:
        payload = {"model": model or settings.chat_model, "messages": messages, "stream": True}
        try:
            with httpx.stream(
                "POST", f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    piece = chunk.get("message", {}).get("content", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama stream failed: {e}") from e

    def embed(self, text: str, model: str | None = None) -> list[float] | None:
        """Return an embedding vector, or None if the embed model isn't available."""
        try:
            r = httpx.post(
                f"{self.base_url}/api/embed",
                json={"model": model or settings.embed_model, "input": text},
                timeout=60,
            )
            r.raise_for_status()
            vecs = r.json().get("embeddings") or []
            return vecs[0] if vecs else None
        except (httpx.HTTPError, ValueError, KeyError):
            return None

    def list_models(self) -> list[str]:
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=10)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except httpx.HTTPError:
            return []


_llm: Ollama | None = None


def get_llm() -> Ollama:
    global _llm
    if _llm is None:
        _llm = Ollama()
    return _llm


def set_llm(llm: Any) -> None:
    """Swap the LLM client (tests use a fake)."""
    global _llm
    _llm = llm
