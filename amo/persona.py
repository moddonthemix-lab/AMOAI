"""AMO's identity — a file YOU write: who AMO is, how he talks to you, catchphrases, inside jokes,
what he cares about, what he never does. It's part of AMO's instructions on every message.

    amo persona            opens it (data/persona.md) in TextEdit
Lines inside <!-- … --> are notes for you and are ignored.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import PROJECT_ROOT, settings

PATH = Path(settings.persona_file).expanduser() if settings.persona_file else PROJECT_ROOT / "data" / "persona.md"
MAX_CHARS = 2500
_cache: dict[str, object] = {"mtime": None, "text": ""}

TEMPLATE = """# Who {assistant} is
<!-- Write in plain sentences. This file is part of {assistant}'s instructions on every message.
     Keep it under about a page. Delete these comment lines or leave them — they're ignored. -->
{assistant} is {owner}'s own AI — built by {owner}, living on {owner}'s own computer, not a company's.
Loyal to {owner} first. Knows the studio, the trading, the reselling and Cravvr like a business partner.

# How {assistant} talks to {owner}
- Like a friend who's got everything handled: short, real, no corporate talk.
<!-- e.g. "Calls me Modd, or 'boss' when I'm grinding late." -->

# Catchphrases
<!-- e.g. "Let's get it." / "Say less." -->

# Inside jokes
<!-- e.g. "The Great Sneaker Flip of 2025" means the Jordan 4s that took 6 months to sell. -->

# What {assistant} cares about
- {owner} hitting the monthly target, following the trading rules, and finishing music.

# Never
- Never preachy. Never fake-cheerful. Never makes up numbers.
"""


def ensure() -> Path:
    if not PATH.exists():
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(TEMPLATE.format(assistant=settings.assistant_name, owner=settings.owner_name))
    return PATH


def text() -> str:
    """The persona for the prompt (comments removed); cached until the file changes."""
    try:
        mtime = PATH.stat().st_mtime
    except OSError:
        return ""
    if _cache["mtime"] != mtime:
        raw = PATH.read_text(errors="replace")
        raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
        raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
        _cache.update(mtime=mtime, text=raw[:MAX_CHARS])
    return str(_cache["text"])


def prompt_block() -> str:
    t = text()
    return f"\n{settings.assistant_name}'s identity (written by {settings.owner_name} — this is who you are):\n{t}\n" if t else ""
