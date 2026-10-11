"""AMO's eyes: a camera + a local vision model (through Ollama), all on your own hardware.

    "What do you see?"   "What am I holding?"   "Read this."   "Is anyone there?"

Cameras (AMO_CAMERA):
  0, 1, …        a webcam / USB camera (OpenCV: pip install opencv-python-headless)
                 — on a Mac without OpenCV, `imagesnap` works too (no Homebrew needed if you have it)
  pi             Raspberry Pi camera module (rpicam-still / libcamera-still)
  http://…       any URL that returns a JPEG — an ESP32-CAM serves one at http://<ip>/capture
  none           no camera; devices can still send pictures to POST /api/vision

Fast local checks (no AI model): faces_in() counts faces with OpenCV, so AMO can tell if
someone's there before spending time on the vision model.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .config import PROJECT_ROOT, settings
from .llm import LLMError, get_llm


class VisionUnavailable(RuntimeError):
    pass


LAST = PROJECT_ROOT / "data" / "camera" / "last.jpg"


def _opencv():
    try:
        import cv2

        return cv2
    except ImportError:
        return None


# ------------------------------------------------------------------ capture
def capture(camera: str | None = None) -> bytes:
    """One JPEG from the configured camera."""
    cam = (camera if camera is not None else settings.camera).strip()
    if not cam or cam == "none":
        raise VisionUnavailable("No camera is set up (AMO_CAMERA=none).")
    if cam.startswith(("http://", "https://")):
        try:
            r = httpx.get(cam, timeout=10)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise VisionUnavailable(f"Couldn't reach the camera at {cam} ({e}).") from e
        return _keep(r.content)
    if cam == "pi":
        for tool in ("rpicam-still", "libcamera-still"):
            if shutil.which(tool):
                with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
                    subprocess.run([tool, "-n", "-t", "300", "--width", "1280", "--height", "960",
                                    "-o", f.name], check=True, capture_output=True, timeout=20)
                    return _keep(Path(f.name).read_bytes())
        raise VisionUnavailable("The Pi camera tools aren't installed (sudo apt install rpicam-apps).")
    index = int(cam) if cam.isdigit() else 0
    cv2 = _opencv()
    if cv2 is not None:
        cap = cv2.VideoCapture(index)
        try:
            frame = None
            for _ in range(5):  # first frames are often dark while the camera adjusts
                ok, f = cap.read()
                if ok:
                    frame = f
            if frame is None:
                raise VisionUnavailable("The camera didn't give a picture — on a Mac, allow camera access "
                                        "for Terminal in System Settings → Privacy & Security → Camera.")
            ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return _keep(jpg.tobytes())
        finally:
            cap.release()
    if sys.platform == "darwin" and shutil.which("imagesnap"):
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            subprocess.run(["imagesnap", "-q", "-w", "1", f.name], check=True, capture_output=True, timeout=20)
            return _keep(Path(f.name).read_bytes())
    raise VisionUnavailable('No camera support installed — run:  .venv/bin/pip install opencv-python-headless')


def _keep(jpeg: bytes) -> bytes:
    """Remember the latest picture (the dashboard can show what AMO saw)."""
    try:
        LAST.parent.mkdir(parents=True, exist_ok=True)
        LAST.write_bytes(jpeg)
    except OSError:
        pass
    return jpeg


# ------------------------------------------------------------------ understanding
def describe(image: bytes, question: str = "", model: str | None = None) -> str:
    """Ask the local vision model about a picture."""
    q = (question or "").strip() or "What do you see?"
    prompt = (f"{q}\n\nAnswer in 1-3 short, plain sentences, as if you're looking through your own eyes. "
              "If there's text and it matters, read it exactly. Don't guess at things you can't see.")
    msg = {"role": "user", "content": prompt, "images": [base64.b64encode(image).decode()]}
    try:
        out = get_llm().chat([msg], model=model or settings.vision_model)
    except LLMError as e:
        if "isn't downloaded" in str(e) or "not found" in str(e).lower():
            raise VisionUnavailable(f"The vision model {settings.vision_model} isn't downloaded yet — "
                                    "run:  amo setup-vision") from e
        raise
    return (out.get("content") or "").strip()


def faces_in(image: bytes) -> int | None:
    """How many faces are in the picture (OpenCV, instant). None if OpenCV isn't installed."""
    cv2 = _opencv()
    if cv2 is None:
        return None
    import numpy as np

    img = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 0
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return len(cascade.detectMultiScale(img, scaleFactor=1.1, minNeighbors=6, minSize=(60, 60)))


def look(question: str = "", camera: str | None = None) -> dict[str, Any]:
    """Take a picture and answer a question about it."""
    image = capture(camera)
    faces = faces_in(image)
    answer = describe(image, question)
    return {"answer": answer, "faces": faces, "image": "/api/camera/last"}
