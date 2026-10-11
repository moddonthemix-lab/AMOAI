# AMO's eyes and hands

AMO's brain (your Mac now, a Raspberry Pi later) does the thinking. Eyes and hands can live
anywhere on your Wi-Fi: an ESP32 board, a Raspberry Pi, or the Mac itself.

| Capability | How AMO does it | Where |
|---|---|---|
| Conversation | Ollama + a local model | brain |
| Speech recognition | Whisper (faster-whisper) | brain, or the Pi body |
| Text-to-speech | Piper | brain, or the Pi body |
| Memory | SQLite | brain |
| Long-term recall | local embeddings (nomic-embed-text) + a vector index over memories and every past conversation | brain |
| Computer vision | camera + OpenCV (face check) + a local vision model in Ollama (moondream / gemma3) | brain; cameras anywhere |
| Hardware control | ESP32 firmware / Pi GPIO agent, talking to the brain over Wi-Fi | ESP32s and Pis |

## Try it now (no hardware)

```sh
amo hw-agent --simulate        # in a second Terminal window: a pretend "desk" board
```
Then tell AMO: "turn on the fan", "make the desk light purple", "turn your head to 120",
"what's the temperature?". The simulated board prints what it does. `amo hw` shows every device.

## Eyes

```sh
amo setup-vision               # downloads the vision model (moondream, ~1.7 GB) and tests the camera
amo look what am I holding     # or just ask AMO: "what do you see?"
```
- Mac webcam: works out of the box (macOS asks for camera access for Terminal the first time).
- Raspberry Pi camera module: `AMO_CAMERA=pi` in `.env`.
- ESP32-CAM (≈$10): flash the standard *CameraWebServer* example, then `AMO_CAMERA=http://<its ip>/capture`.
- Any device can also send a picture: `POST /api/vision` (form fields `file`, `question`).
- Better eyes, slower: `amo setup-vision gemma3:4b`.

The last picture AMO looked at is at `/api/camera/last`.

## Hands: ESP32

1. Arduino IDE → Boards Manager: **esp32** (Espressif). Library Manager: **ArduinoJson**, **ESP32Servo**,
   **Adafruit NeoPixel** (and **TFT_eSPI** for a screen).
2. Open `firmware/esp32/amo_body/amo_body.ino`, edit `config.h`:
   Wi-Fi, `AMO_BRAIN` (run `amo lan on` on the Mac — it prints the address and key), `AMO_KEY`,
   `DEVICE_NAME`, and the `PARTS` table — what's wired to which pin.
3. Upload. The serial monitor says `AMO knows this board as "desk"`.

Part types: `SWITCH` (relay, fan, LED), `RGB` (NeoPixel strip), `SERVO`, `BUZZER`,
`SENSOR` (analog pin with scale/offset, or the chip's own temperature).

**AMO's face on a screen:** set up TFT_eSPI for your display (`User_Setup.h`), uncomment `#define AMO_FACE`
in `config.h`. The board draws AMO's eyes and talking mouth from `/api/face/now`, coloured by mood.

Tested to compile for ESP32 and ESP32-S3 (arduino-esp32 3.0).

## Hands: Raspberry Pi GPIO

```sh
amo hw-agent --config hw.json --brain http://Your-Mac.local:8765 --key THE_KEY
```
`hw.json`:
```json
{"device": "amo-body",
 "parts": [{"id": "fan",   "type": "switch", "pin": 17},
           {"id": "light", "type": "rgb",    "pins": [22, 23, 24], "name": "body light"},
           {"id": "head",  "type": "servo",  "pin": 18},
           {"id": "temp",  "type": "sensor", "source": "cpu", "unit": "°C"}]}
```
Uses gpiozero (already on Raspberry Pi OS). Run it next to `amo device` on the same Pi.

## The protocol (for anything else you build)

```
POST /api/hw/hello   {"device": "desk", "kind": "esp32", "parts": [{"id": "fan", "type": "switch"}, …]}
GET  /api/hw/poll?device=desk&wait=20      → {"commands": [{"id": 7, "part": "fan", "action": "on", "value": null}]}
POST /api/hw/report  {"device": "desk", "state": {"fan": true, "temp": 23.4}, "done": [{"id": 7, "ok": true}]}
GET  /api/hw                               → every device, online or not, its parts and state
POST /api/hw/command {"target": "fan", "action": "on"}
GET  /api/face/now                         → {"state", "speaking", "level", "word", "mood"} for small screens
```
Every call takes `Authorization: Bearer <AMO_API_KEY>` from other devices. AMO only says something is done
once the device confirms it; if a board is offline, the command waits and runs when it reconnects.
