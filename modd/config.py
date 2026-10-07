"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so we don't need python-dotenv. Existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(Path(os.environ.get("MODD_ENV_FILE", ".env")))


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: _env("MODD_DB_PATH", "./data/modd.db"))
    owner_name: str = field(default_factory=lambda: _env("MODD_OWNER_NAME", "Modd"))
    assistant_name: str = field(default_factory=lambda: _env("MODD_ASSISTANT_NAME", "Modd"))
    timezone: str = field(default_factory=lambda: _env("MODD_TIMEZONE", "America/New_York"))
    api_key: str = field(default_factory=lambda: _env("MODD_API_KEY", ""))

    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    chat_model: str = field(default_factory=lambda: _env("MODD_CHAT_MODEL", "llama3.1:8b"))
    fast_model: str = field(default_factory=lambda: _env("MODD_FAST_MODEL", "llama3.2:3b"))
    embed_model: str = field(default_factory=lambda: _env("MODD_EMBED_MODEL", "nomic-embed-text"))

    whisper_model: str = field(default_factory=lambda: _env("MODD_WHISPER_MODEL", "base.en"))
    piper_voice: str = field(
        default_factory=lambda: _env("MODD_PIPER_VOICE", "./models/piper/en_US-lessac-medium.onnx")
    )

    ntfy_url: str = field(default_factory=lambda: _env("MODD_NTFY_URL", ""))
    ntfy_topic: str = field(default_factory=lambda: _env("MODD_NTFY_TOPIC", "modd"))

    # Monthly revenue target shown on the dashboard (0 = none).
    monthly_revenue_target: float = field(
        default_factory=lambda: float(_env("MODD_MONTHLY_REVENUE_TARGET", "0"))
    )

    # How many memories to inject into each conversation.
    memory_context_limit: int = field(default_factory=lambda: int(_env("MODD_MEMORY_LIMIT", "8")))
    # Learn facts from conversations automatically.
    auto_learn: bool = field(default_factory=lambda: _env("MODD_AUTO_LEARN", "1") not in ("0", "false", "no"))


settings = Settings()
