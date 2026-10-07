"""Desktop voice mode: press Enter, talk, AMO answers out loud.

Recording stops automatically after ~1s of silence. (Phase 2 swaps the Enter key
for a "Hey AMO" wake word on the Raspberry Pi — the rest of this loop stays the same.)
"""

from __future__ import annotations

import io
import wave

from ..agent import Agent
from .stt import VoiceUnavailable, transcribe_array
from .tts import synthesize

SAMPLE_RATE = 16000
BLOCK = 1600  # 100 ms


def record_utterance(max_seconds: float = 30, silence_seconds: float = 1.0, threshold: float = 0.01):
    """Record from the default mic until the speaker goes quiet. Returns float32 samples."""
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as e:
        raise VoiceUnavailable('pip install -e ".[voice]" for microphone support') from e

    chunks: list = []
    heard_speech = False
    quiet_blocks = 0
    needed_quiet = int(silence_seconds * SAMPLE_RATE / BLOCK)
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK) as stream:
        for _ in range(int(max_seconds * SAMPLE_RATE / BLOCK)):
            block, _ = stream.read(BLOCK)
            block = block[:, 0]
            chunks.append(block.copy())
            loud = float(np.sqrt(np.mean(block**2))) > threshold
            if loud:
                heard_speech, quiet_blocks = True, 0
            elif heard_speech:
                quiet_blocks += 1
                if quiet_blocks >= needed_quiet:
                    break
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype="float32")


def play_wav(data: bytes) -> None:
    import numpy as np
    import sounddevice as sd

    with wave.open(io.BytesIO(data)) as w:
        frames = w.readframes(w.getnframes())
        rate = w.getframerate()
    audio = np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768
    sd.play(audio, rate)
    sd.wait()


def run(speak: bool = True) -> None:
    agent = Agent()
    history: list[dict] = []
    print("Voice mode. Press Enter and speak (Ctrl+C to quit).")
    while True:
        try:
            input("\n[Enter to talk] ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        print("listening…")
        text = transcribe_array(record_utterance(), SAMPLE_RATE)
        if not text:
            print("(didn't catch that)")
            continue
        print(f"you: {text}")
        history.append({"role": "user", "content": text})
        reply = agent.chat(history[-12:], channel="voice")["content"]
        history.append({"role": "assistant", "content": reply})
        print(f"amo: {reply}")
        if speak and reply:
            play_wav(synthesize(reply))
