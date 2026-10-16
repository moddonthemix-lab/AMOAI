"""AMO speaking up out loud on his own, from the server — no dashboard page or hands-free listening needed.

Everything AMO queues to say (hub news, Strat level alerts, timers, reminders, studio heads-ups, the
morning brief) is spoken through the Mac's speakers in his own voice within a few seconds. Quiet hours
are respected (only timers and urgent things then). When hands-free listening (`amo listen`) is running,
it does the talking instead, so the two never talk over each other. AMO_SPEAK_ALOUD=0 turns this off.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import settings

log = logging.getLogger(__name__)

LISTENER_ALIVE = 30  # seconds: a hands-free listener that checked in this recently does the talking


def enabled() -> bool:
    v = os.environ.get("AMO_SPEAK_ALOUD", "").strip().lower()
    if v:
        return v not in ("0", "false", "no", "off")
    return sys.platform == "darwin"  # the Mac with speakers; off elsewhere unless asked for


def _alive_file() -> Path:
    return Path(settings.db_path).parent / "listener.alive"


def listener_checked_in() -> None:
    """Called by hands-free listening each time it looks for things to say."""
    try:
        p = _alive_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    except OSError:
        pass


def listener_running() -> bool:
    try:
        return time.time() - _alive_file().stat().st_mtime < LISTENER_ALIVE
    except OSError:
        return False


def play(wav: bytes) -> None:
    """Through the Mac's speakers (afplay), or sounddevice elsewhere."""
    if shutil.which("afplay"):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
        try:
            subprocess.run(["afplay", f.name], check=False, timeout=300)
        finally:
            os.unlink(f.name)
        return
    from .voice.loop import play_wav

    play_wav(wav)


class Announcer:
    def __init__(self, interval: float = 4.0, take: Callable[[], list[str]] | None = None,
                 speak: Callable[[str], None] | None = None):
        self.interval = interval
        self._take = take
        self._speak = speak
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="amo-announcer", daemon=True)
        self._chime: bytes | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def take(self) -> list[str]:
        if self._take:
            return self._take()
        from .proactive import take_pending

        return take_pending()

    def say(self, text: str) -> None:
        if self._speak:
            return self._speak(text)
        from . import face
        from .voice.tts import synthesize

        if self._chime is None:
            try:
                from .voice.listen import chime_wav

                self._chime = chime_wav()
            except Exception:  # noqa: BLE001 — no numpy: no chime, still speak
                self._chime = b""
        if self._chime:
            play(self._chime)
        wav = synthesize(text)
        face.publish_say(text, wav)  # the face page moves its mouth if it's open
        play(wav)

    def tick(self) -> int:
        """Say what's waiting. Returns how many were said."""
        if listener_running():
            return 0  # hands-free listening speaks them (and knows when you're mid-conversation)
        said = 0
        for text in self.take():
            try:
                self.say(text)
                said += 1
            except Exception:  # noqa: BLE001 — a voice problem must never stop the announcer
                log.exception("couldn't say an announcement")
        return said

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("announcer tick failed")


_running: Announcer | None = None


def start() -> Any:
    global _running
    if enabled() and _running is None:
        _running = Announcer()
        _running.start()
    return _running


def stop() -> None:
    global _running
    if _running:
        _running.stop()
        _running = None
