FROM python:3.12-slim

# ffmpeg lets Whisper decode browser audio (webm/ogg) from Open WebUI's mic button.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY modd ./modd
RUN pip install --no-cache-dir faster-whisper piper-tts && pip install --no-cache-dir -e .

ENV MODD_DB_PATH=/data/modd.db \
    MODD_PIPER_VOICE=/models/piper/en_US-lessac-medium.onnx \
    HF_HOME=/models/hf
VOLUME ["/data", "/models"]
EXPOSE 8765
HEALTHCHECK CMD curl -fs http://localhost:8765/api/health || exit 1
CMD ["sh", "-c", "[ -f \"$MODD_PIPER_VOICE\" ] || modd setup-voice; modd serve --port 8765"]
