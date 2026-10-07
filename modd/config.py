"""Runtime configuration, read from environment variables.

Everything has a sane default so `modd chat` works out of the box on a
desktop running Ollama. The same settings carry over to the Raspberry Pi
(Phase 2) — only the model name usually changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_home() -> Path:
    return Path(os.environ.get("MODD_HOME", Path.home() / ".modd")).expanduser()


@dataclass
class Config:
    home: Path = field(default_factory=_default_home)
    ollama_url: str = field(default_factory=lambda: os.environ.get("MODD_OLLAMA_URL", "http://localhost:11434"))
    model: str = field(default_factory=lambda: os.environ.get("MODD_MODEL", "llama3.1:8b"))
    assistant_name: str = field(default_factory=lambda: os.environ.get("MODD_NAME", "Modd"))
    user_name: str = field(default_factory=lambda: os.environ.get("MODD_USER", "boss"))
    # Voice (optional extras)
    whisper_model: str = field(default_factory=lambda: os.environ.get("MODD_WHISPER_MODEL", "base.en"))
    piper_voice: str = field(default_factory=lambda: os.environ.get("MODD_PIPER_VOICE", ""))
    # HTTP API (used by the ESP32 dashboard in Phase 3)
    api_host: str = field(default_factory=lambda: os.environ.get("MODD_API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: int(os.environ.get("MODD_API_PORT", "8765")))
    api_token: str = field(default_factory=lambda: os.environ.get("MODD_API_TOKEN", ""))

    @property
    def db_path(self) -> Path:
        return self.home / "modd.db"

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
