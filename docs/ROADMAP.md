# Modd roadmap

## ✅ Phase 1 — Desktop (this repo, today)
- Local AI on your desktop PC (Ollama), Open WebUI as the interface
- Long-term memory in SQLite (keyword + embedding search, auto-learning, weekly reflection)
- Voice: Whisper speech-to-text, Piper text-to-speech (Open WebUI mic + `modd voice`)
- Studio CRM, reselling tracker, trading journal, goals, Cravvr tasks, revenue tracking
- Built-in dashboard + morning brief + session reminders (+ optional phone push via ntfy)

## ⏭ Phase 2 — Raspberry Pi 5, always on
What's already in place:
- Everything is plain Python + SQLite + Docker — the same `docker-compose.yml` runs on a Pi 5 (arm64).
- `modd/voice/loop.py` is the voice loop; only the trigger changes (Enter key → wake word).
- The scheduler already runs inside the server, so reminders/briefs work 24/7 once it's always on.

To do:
- [ ] Pi 5 (8GB) install guide + systemd service (smaller model: `llama3.2:3b` or `qwen2.5:3b` as chat model)
- [ ] Wake word "Hey Modd": openWakeWord with a custom-trained model (use `hey_jarvis` until trained)
- [ ] USB mic / speaker setup, voice loop as a service
- [ ] Optional: keep the big model on the desktop and point the Pi's `OLLAMA_URL` at it over LAN
- [ ] Move `data/modd.db` to the Pi (single file copy) — backups via cron

## ⏭ Phase 3 — ESP32 touchscreen
What's already in place:
- `GET /api/dashboard` returns a small JSON snapshot designed for an ESP32 (today's sessions,
  goals + streaks, revenue today/week/month + 14-day sparkline, trading P&L, reselling, notifications).
- `POST /api/goals/{id}/checkin` and `POST /api/notifications/read` for tapping things on the screen.
- Bearer-token auth (`MODD_API_KEY`) the device can send.

To do:
- [ ] ESP32-S3 + touchscreen firmware (LVGL / TFT_eSPI) polling `/api/dashboard`
- [ ] Studio dashboard screen, daily goals screen with tap-to-check-in, revenue screen
- [ ] Client notifications on the device (session reminders, unpaid balances)
- [ ] Optional: push-to-talk button on the device streaming audio to `/v1/audio/transcriptions`
