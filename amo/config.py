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


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def env_file() -> Path:
    """The .env in use: $AMO_ENV_FILE, else ./.env, else the one in the AMO folder."""
    if os.environ.get("AMO_ENV_FILE"):
        return Path(os.environ["AMO_ENV_FILE"])
    local = Path(".env")
    return local if local.is_file() else PROJECT_ROOT / ".env"


_load_dotenv(env_file())


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: _env("AMO_DB_PATH", "./data/amo.db"))
    owner_name: str = field(default_factory=lambda: _env("AMO_OWNER_NAME", "Modd"))
    assistant_name: str = field(default_factory=lambda: _env("AMO_ASSISTANT_NAME", "AMO"))
    timezone: str = field(default_factory=lambda: _env("AMO_TIMEZONE", "America/New_York"))
    api_key: str = field(default_factory=lambda: _env("AMO_API_KEY", "").strip())
    # Requests from this same computer (127.0.0.1) don't need the API key.
    trust_localhost: bool = field(
        default_factory=lambda: _env("AMO_TRUST_LOCALHOST", "1") not in ("0", "false", "no")
    )

    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    chat_model: str = field(default_factory=lambda: _env("AMO_CHAT_MODEL", "llama3.1:8b"))
    fast_model: str = field(default_factory=lambda: _env("AMO_FAST_MODEL", "llama3.2:3b"))
    embed_model: str = field(default_factory=lambda: _env("AMO_EMBED_MODEL", "nomic-embed-text"))

    # Voice: a preset (amo | computer | jarvis | british-female | default), a Piper voice name,
    # or "say:<macOS voice>". Rate/pitch/robot override the preset when set.
    voice: str = field(default_factory=lambda: _env("AMO_VOICE", "amo"))
    voice_rate: str = field(default_factory=lambda: _env("AMO_VOICE_RATE", ""))
    voice_pitch: str = field(default_factory=lambda: _env("AMO_VOICE_PITCH", ""))
    voice_robot: str = field(default_factory=lambda: _env("AMO_VOICE_ROBOT", ""))
    # Pause between sentences (seconds) and target loudness (RMS, 0.05 quiet … 0.15 loud).
    voice_sentence_pause: float = field(default_factory=lambda: float(_env("AMO_VOICE_PAUSE", "0.28")))
    voice_loudness: float = field(default_factory=lambda: float(_env("AMO_VOICE_LOUDNESS", "0.1")))
    # Personality: default | computer (dry, sarcastic British computer) | jarvis (polished butler).
    # Empty = whatever suits the voice preset (AMO's own voice → computer).
    personality: str = field(default_factory=lambda: _env("AMO_PERSONALITY", ""))

    # Spoken acknowledgements while AMO thinks (phrases separated by |).
    acks: bool = field(default_factory=lambda: _env("AMO_ACKS", "1") not in ("0", "false", "no"))
    ack_think: str = field(default_factory=lambda: _env(
        "AMO_ACK_THINK", "Okay, let me think.|Let me think.|One moment."))
    ack_action: str = field(default_factory=lambda: _env(
        "AMO_ACK_ACTION", "Got it.|I'll work on that now.|On it."))
    # Said if an answer is still being worked on after AMO_ACK_STILL_AFTER seconds (one per wait).
    ack_still: str = field(default_factory=lambda: _env("AMO_ACK_STILL", "Still working on it.|Almost there."))
    ack_still_after: float = field(default_factory=lambda: float(_env("AMO_ACK_STILL_AFTER", "7")))

    # AMO speaking up on its own (watchlist alerts, session heads-ups, brief, check-in),
    # never inside quiet hours (e.g. "22-8" = 10pm–8am). Evening check-in hour (24h).
    proactive: bool = field(default_factory=lambda: _env("AMO_PROACTIVE", "1") not in ("0", "false", "no"))
    quiet_hours: str = field(default_factory=lambda: _env("AMO_QUIET_HOURS", "22-8"))
    checkin_hour: int = field(default_factory=lambda: int(_env("AMO_CHECKIN_HOUR", "21")))

    # Speak while thinking (voice starts on the first finished sentence) and let you interrupt
    # AMO mid-answer ("AMO, stop", "hold on", "never mind").
    stream_speech: bool = field(default_factory=lambda: _env("AMO_STREAM_SPEECH", "1") not in ("0", "false", "no"))
    barge_in: bool = field(default_factory=lambda: _env("AMO_BARGE_IN", "1") not in ("0", "false", "no"))

    # Wake word ("Hey AMO"): a tiny Whisper model checks short bursts of speech for the phrase.
    wake_model: str = field(default_factory=lambda: _env("AMO_WAKE_MODEL", "tiny.en"))
    # Extra spellings Whisper might hear for "AMO", comma-separated (e.g. "amore,emu").
    wake_words: str = field(default_factory=lambda: _env("AMO_WAKE_WORDS", ""))
    # Mic sensitivity: how many times louder than the room you must be (1.5 = very sensitive, 4 = strict).
    wake_sensitivity: float = field(default_factory=lambda: float(_env("AMO_WAKE_SENSITIVITY", "2")))

    whisper_model: str = field(default_factory=lambda: _env("AMO_WHISPER_MODEL", "base.en"))
    piper_voice: str = field(
        default_factory=lambda: _env("AMO_PIPER_VOICE", "./models/piper/en_US-lessac-medium.onnx")
    )

    ntfy_url: str = field(default_factory=lambda: _env("AMO_NTFY_URL", ""))
    ntfy_topic: str = field(default_factory=lambda: _env("AMO_NTFY_TOPIC", "amo"))

    # Model runtime: context window (tokens), how long Ollama keeps the model loaded,
    # and whether "thinking" models (qwen3, gemma4…) may think before answering (slow on CPU).
    num_ctx: int = field(default_factory=lambda: int(_env("AMO_NUM_CTX", "8192")))
    keep_alive: str = field(default_factory=lambda: _env("AMO_KEEP_ALIVE", "30m"))
    think: bool = field(default_factory=lambda: _env("AMO_THINK", "0") in ("1", "true", "yes"))

    # Web search: blank = DuckDuckGo (no key). Or your own SearXNG, e.g. http://localhost:8888
    searxng_url: str = field(default_factory=lambda: _env("AMO_SEARXNG_URL", ""))

    # Monthly revenue target shown on the dashboard (0 = none).
    monthly_revenue_target: float = field(
        default_factory=lambda: float(_env("AMO_MONTHLY_REVENUE_TARGET", "0"))
    )

    # How many memories to inject into each conversation.
    memory_context_limit: int = field(default_factory=lambda: int(_env("AMO_MEMORY_LIMIT", "8")))
    # Learn facts from conversations automatically.
    auto_learn: bool = field(default_factory=lambda: _env("AMO_AUTO_LEARN", "1") not in ("0", "false", "no"))


settings = Settings()
