# AMO — your to-do list

Things only you can do (AMO / Claude can't do them for you). Tick them off as you go.

## On your Mac — next time you sit down
- [ ] Update AMO: `cd ~/AMO && ./scripts/update-mac.sh` (also downloads the code model once, ~2 GB)
- [ ] Set your city for weather + severe-weather alerts: in `~/AMO/.env` set `AMO_LOCATION="Your City, ST"`,
      then check with `.venv/bin/amo brief`
- [ ] Write AMO's identity: `.venv/bin/amo persona` (who he is, how he talks to you, catchphrases, inside jokes, never-dos)
- [ ] Give him your documents: `.venv/bin/amo knowledge` → drop PDFs, docs, price sheets, plans, lyrics into
      `~/Documents/AMO Knowledge`
- [ ] Optional: add your calendar's private link to `AMO_CALENDAR_URLS` in `.env` (iCloud: share calendar → Public
      Calendar → copy the webcal:// link; Google: Settings → calendar → "Secret address in iCal format")

## Tuning — send the output back so AMO can be tuned on real numbers
- [x] Compare AI models on the test set (llama3.2:3b most accurate but ~25s; gemma4:e2b ~9s)
- [ ] Re-run with the per-kind breakdown, then switch on the two-tier brain:
      `.venv/bin/amo eval --models llama3.2:3b,gemma4:e2b` → `.venv/bin/amo brain --auto`
- [ ] qwen3:4b didn't run in the eval — check `ollama list`; if it's there, the re-run shows the error
- [ ] After a day or two of normal use: `.venv/bin/amo stats` (speed report) and `.venv/bin/amo mistakes`
- [ ] Try the checklist: "what's 15% of 240", "set a timer for 1 minute" (with Face ↗ open),
      "note: test idea", "from now on keep it short", "what do my docs say about …", "what's your input on
      Amazon or Tesla", a coding question, interrupt him with "no, I said …", and `amo hw-agent --simulate` + "turn on the fan"
- [ ] Note anything slow, misheard or wrong (or paste errors) and send it over

## Later
- [ ] Pocket pet body (DIY): buy parts when ready — XIAO ESP32S3 Sense, a separate 1.28" GC9A01 round display
      module (not the stacking "Round Display for XIAO"), MAX98357A amp + small speaker, 2 buttons, LiPo,
      microSD card. Ask for the full wiring/build guide first.
- [ ] Tailscale (free) on the Mac + phone, so AMO's bodies reach him away from home
- [ ] Apple Silicon Mac mini (biggest speed upgrade) or Raspberry Pi 5 as an always-on brain

## Printer (HP on Wi-Fi)
- [ ] Mac: System Settings → Printers & Scanners → Add Printer… → pick the HP (same Wi-Fi)
- [ ] `.venv/bin/amo printer --test` — shows printers + ink, prints a test page
- [ ] Try: "print my schedule for tomorrow", "print Lil Jay's invoice", "print that", "how much ink is left?"
