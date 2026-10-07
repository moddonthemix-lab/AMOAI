"""Thin client for a local Ollama server (https://ollama.com)."""

from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

from .config import settings


class LLMError(RuntimeError):
    pass


# Model families that "think" before answering. Thinking is great on a GPU but adds a long
# delay on CPU, so AMO turns it off unless AMO_THINK=1.
_THINKING_FAMILIES = ("qwen3", "gemma4", "deepseek-r1", "magistral", "phi4-reasoning")


def runtime_params(model: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "options": {"num_ctx": settings.num_ctx, **(options or {})},
        "keep_alive": settings.keep_alive,
    }
    if model.split("/")[-1].startswith(_THINKING_FAMILIES):
        params["think"] = settings.think
    return params


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
            **runtime_params(model or settings.chat_model, options),
        }
        if tools:
            payload["tools"] = tools
        if fmt:
            payload["format"] = fmt
        try:
            r = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise LLMError(f"Can't reach Ollama at {self.base_url} — is the Ollama app running? ({e})") from e
        if r.status_code == 400 and "does not support tools" in r.text:
            raise LLMError(
                f"The model '{payload['model']}' can't use tools, so AMO can't save anything with it. "
                "Pick a model with tool support, e.g.  amo use llama3.2:3b  or  amo use gemma4:e2b"
            )
        if r.status_code == 404:
            raise LLMError(
                f"The AI model '{payload['model']}' isn't downloaded yet. "
                f"In Terminal run:  ollama pull {payload['model']}   (or ./scripts/pull-models.sh)"
            )
        if r.is_error:
            raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return r.json().get("message", {"role": "assistant", "content": ""})

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Streaming chat. Yields {"content": piece} as text arrives and {"tool_calls": [...]}
        if the model decides to call tools."""
        payload: dict[str, Any] = {"model": model or settings.chat_model, "messages": messages, "stream": True,
                                   **runtime_params(model or settings.chat_model)}
        if tools:
            payload["tools"] = tools
        try:
            with httpx.stream("POST", f"{self.base_url}/api/chat", json=payload, timeout=self.timeout) as r:
                if r.status_code == 404:
                    raise LLMError(f"The AI model '{payload['model']}' isn't downloaded yet. "
                                   f"In Terminal run:  ollama pull {payload['model']}")
                if r.status_code == 400:
                    r.read()
                    if "does not support tools" in r.text:
                        raise LLMError(f"The model '{payload['model']}' can't use tools. Try: amo use llama3.2:3b")
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    msg = chunk.get("message", {})
                    if msg.get("tool_calls"):
                        yield {"tool_calls": msg["tool_calls"]}
                    if msg.get("content"):
                        yield {"content": msg["content"]}
                    if chunk.get("done"):
                        break
        except httpx.HTTPError as e:
            raise LLMError(f"Can't reach Ollama at {self.base_url} — is the Ollama app running? ({e})") from e

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

    def pull(self, model: str) -> Iterator[str]:
        """Download a model, yielding progress lines."""
        with httpx.stream("POST", f"{self.base_url}/api/pull", json={"model": model, "stream": True},
                          timeout=None) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                if d.get("error"):
                    raise LLMError(d["error"])
                status = d.get("status", "")
                if d.get("total") and d.get("completed") is not None:
                    status += f" {d['completed'] * 100 // d['total']}%"
                yield status

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
