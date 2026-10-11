"""Hardware agent for a Raspberry Pi (or a simulated device on your Mac): speaks the same protocol
as the ESP32 firmware, so AMO can control the Pi's GPIO pins.

    amo hw-agent --simulate                       # try it now: a pretend "desk" with a light, fan, temp
    amo hw-agent --config hw.json --brain http://Your-Mac.local:8765 --key KEY    # on the Pi

hw.json:
    {"device": "amo-body",
     "parts": [{"id": "fan",   "type": "switch", "pin": 17},
               {"id": "light", "type": "rgb",    "pins": [22, 23, 24], "name": "body light"},
               {"id": "head",  "type": "servo",  "pin": 18},
               {"id": "temp",  "type": "sensor", "source": "cpu", "unit": "°C"}]}

On the Pi this uses gpiozero (pre-installed on Raspberry Pi OS).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx

SIM_CONFIG = {
    "device": "desk",
    "kind": "sim",
    "parts": [{"id": "light", "type": "rgb", "name": "desk light"},
              {"id": "fan", "type": "switch"},
              {"id": "head", "type": "servo"},
              {"id": "temp", "type": "sensor", "unit": "°C", "source": "sim"}],
}


class Part:
    """One controllable thing. apply() does the command and returns the new state value."""

    def __init__(self, spec: dict[str, Any], simulate: bool, log: Callable[[str], None]):
        self.spec, self.id, self.type = spec, spec["id"], spec["type"]
        self.log, self.dev = log, None
        self.value: Any = {"switch": False, "rgb": "#000000", "servo": 90, "motor": 0}.get(self.type)
        if not simulate:
            self.dev = self._gpio()

    def _gpio(self):
        import gpiozero  # noqa: F401 — only on the Pi

        from gpiozero import AngularServo, Buzzer, OutputDevice, RGBLED

        s = self.spec
        if self.type == "switch":
            return OutputDevice(s["pin"], active_high=s.get("active_high", True))
        if self.type == "rgb":
            return RGBLED(*s["pins"])
        if self.type == "servo":
            return AngularServo(s["pin"], min_angle=0, max_angle=180)
        if self.type == "buzzer":
            return Buzzer(s["pin"])
        return None

    def apply(self, action: str, value: Any) -> Any:
        t = self.type
        if t == "switch":
            on = {"on": True, "off": False}.get(action, not self.value)
            if self.dev:
                self.dev.on() if on else self.dev.off()
            self.value = on
        elif t == "rgb":
            if action == "off":
                colour = "#000000"
            elif action == "on":
                colour = "#ffffff" if self.value == "#000000" else self.value
            elif action == "brightness":
                colour = self.value if self.value != "#000000" else "#ffffff"
            else:
                colour = value
            if self.dev:
                r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
                k = (value / 100) if action == "brightness" else 1
                self.dev.color = (r * k, g * k, b * k)
            self.value = colour
        elif t == "servo":
            if value is None:
                raise ValueError("what angle?")
            if self.dev:
                self.dev.angle = float(value)
            self.value = int(value)
        elif t in ("motor", "display"):  # not wired up on the Pi agent yet — say so, don't pretend
            raise ValueError(f"{t} parts aren't supported by this agent yet")
        elif t == "buzzer":
            if self.dev:
                self.dev.beep(0.1, 0.1, n=2)
        elif t == "sensor":
            raise ValueError("sensors are read-only")
        self.log(f"   ⚙ {self.id}: {action} {'' if value is None else value} → {self.value}")
        return self.value

    def read(self) -> Any:
        if self.type != "sensor":
            return self.value
        src = self.spec.get("source", "cpu")
        if src == "cpu":
            try:
                return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
            except OSError:
                return None
        if src == "sim":
            return round(21.5 + (time.time() % 60) / 30, 1)
        return None


def run(brain: str, key: str = "", config: dict[str, Any] | None = None, simulate: bool = False,
        log: Callable[[str], None] = print, once: bool = False) -> None:
    cfg = config or SIM_CONFIG
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    parts = {p["id"]: Part(p, simulate, log) for p in cfg["parts"]}
    name = cfg["device"]

    def state() -> dict[str, Any]:
        return {pid: p.read() for pid, p in parts.items()}

    def say_hello() -> None:
        r = httpx.post(f"{brain}/api/hw/hello", headers=headers, timeout=10,
                       json={"device": name, "kind": cfg.get("kind", "sim" if simulate else "pi"),
                             "parts": cfg["parts"], "state": state()})
        r.raise_for_status()

    say_hello()
    log(f"⚙ '{name}' connected to AMO at {brain} — parts: {', '.join(parts)}. Try: \"turn on the fan\"")
    last_report = 0.0
    while True:
        try:
            r = httpx.get(f"{brain}/api/hw/poll", params={"device": name, "wait": 20}, headers=headers, timeout=30)
            r.raise_for_status()
            done = []
            for c in r.json()["commands"]:
                try:
                    parts[c["part"]].apply(c["action"], c["value"])
                    done.append({"id": c["id"], "ok": True})
                except Exception as e:  # noqa: BLE001
                    done.append({"id": c["id"], "ok": False, "error": str(e)})
            if done or time.monotonic() - last_report > 10:
                httpx.post(f"{brain}/api/hw/report", headers=headers, timeout=10,
                           json={"device": name, "state": state(), "done": done}).raise_for_status()
                last_report = time.monotonic()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                say_hello()
                continue
            log(f"   ✗ {e}")
            time.sleep(3)
        except httpx.HTTPError as e:
            log(f"   ✗ can't reach AMO ({e}) — retrying")
            time.sleep(3)
        if once:
            return


def load_config(path: str) -> dict[str, Any]:
    return json.loads(Path(path).expanduser().read_text())
