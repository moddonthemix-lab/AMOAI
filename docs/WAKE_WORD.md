# "Hey AMO" wake word — choosing an engine

| Engine | Where it fits | Setup |
|---|---|---|
| `whisper` (default) | Mac. Most accurate; checks each burst of speech with a tiny Whisper model. | nothing |
| `vosk` | Raspberry Pi / body device. Only listens for "Hey AMO" (grammar-limited), very light, no training. Needs "**Hey** AMO" (not just "AMO"). | `amo setup-wake vosk`, then `AMO_WAKE_ENGINE=vosk` |
| `openwakeword` | Smallest hardware, lowest power, best accuracy once trained on "Hey AMO". | train a model (below), then `AMO_WAKE_ENGINE=openwakeword` |

With `vosk` or `openwakeword` on a body device, only what you say *after* "Hey AMO" is sent to the
brain — the device isn't streaming the room to your Mac.

## Training your own "Hey AMO" model (openWakeWord)

openWakeWord can train a custom wake word from synthetic speech — no recordings needed
(it uses Piper voices, like AMO's).

1. `amo setup-wake openwakeword` (installs the library).
2. Open the openWakeWord project on GitHub (github.com/dscripka/openWakeWord) and follow its
   **training a new model** guide — it links a ready-made Google Colab notebook. Enter the phrase
   **"hey amo"** (add a spelling variant like **"hey ah mo"** if offered), run it (free Colab is
   fine, roughly an hour), and download the resulting model file.
3. Save it as `~/AMO/models/wake/hey_amo.onnx` (or `.tflite`; set `AMO_OWW_MODEL` to the path).
4. In `~/AMO/.env`: `AMO_WAKE_ENGINE=openwakeword`. Tune `AMO_OWW_THRESHOLD` (0.5 default; higher =
   fewer false wakes, lower = more sensitive).
5. Restart AMO Listen (or `amo device …`).

Tip: record yourself saying "Hey AMO" a few times in the room the device lives in and check it with
`amo listen -v` — it prints each wake.
