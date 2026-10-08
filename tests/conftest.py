import os

os.environ["AMO_ENV_FILE"] = "/nonexistent"
os.environ["AMO_API_KEY"] = ""
os.environ["AMO_AUTO_LEARN"] = "0"
os.environ["AMO_VOICE"] = "default"
os.environ["AMO_BACKUP_DIR"] = __import__("tempfile").mkdtemp(prefix="amo-test-backups-")

import pytest  # noqa: E402

from amo.db import Database, set_db  # noqa: E402
from amo.llm import set_llm  # noqa: E402


class FakeLLM:
    """Scripted stand-in for Ollama. Queue responses with .script(...)."""

    def __init__(self):
        self.queue: list[dict] = []
        self.calls: list[dict] = []

    def script(self, *messages: dict) -> None:
        self.queue.extend(messages)

    def chat(self, messages, model=None, tools=None, options=None, fmt=None):
        self.calls.append({"messages": messages, "model": model, "tools": tools, "fmt": fmt})
        if self.queue:
            return self.queue.pop(0)
        return {"role": "assistant", "content": "ok"}

    def stream_chat(self, messages, model=None, tools=None):
        """Streams the next scripted reply word by word (tool calls come through whole)."""
        msg = self.chat(messages, model=model, tools=tools)
        if msg.get("tool_calls"):
            yield {"tool_calls": msg["tool_calls"]}
        words = msg.get("content", "").split(" ")
        for i, w in enumerate(words):
            yield {"content": w + (" " if i < len(words) - 1 else "")}

    def embed(self, text, model=None):
        return None

    def list_models(self):
        return []


@pytest.fixture()
def db():
    d = Database(":memory:")
    set_db(d)
    return d


@pytest.fixture()
def llm():
    f = FakeLLM()
    set_llm(f)
    return f


def tool_call(name: str, **args) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}
