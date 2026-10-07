import os

os.environ["MODD_ENV_FILE"] = "/nonexistent"
os.environ["MODD_API_KEY"] = ""
os.environ["MODD_AUTO_LEARN"] = "0"

import pytest  # noqa: E402

from modd.db import Database, set_db  # noqa: E402
from modd.llm import set_llm  # noqa: E402


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
