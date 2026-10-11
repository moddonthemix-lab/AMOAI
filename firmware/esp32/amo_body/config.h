// ---- AMO body (ESP32) settings — edit these, then upload with the Arduino IDE ----
#pragma once

// Wi-Fi and AMO's brain (your Mac, or the Raspberry Pi running `amo serve`)
#define WIFI_SSID     "YourWiFi"
#define WIFI_PASSWORD "YourPassword"
#define AMO_BRAIN     "http://192.168.1.50:8765"   // `amo lan on` prints this address
#define AMO_KEY       ""                           // the brain's AMO_API_KEY ("" if none)
#define DEVICE_NAME   "desk"                       // how AMO knows this board

// Optional: AMO's face on a TFT screen (ESP32 + ILI9341/ST7789 etc. via TFT_eSPI).
// Set up TFT_eSPI's User_Setup.h for your screen first, then uncomment:
// #define AMO_FACE

// ---- Parts: what's wired to this board ----
// type: SWITCH (relay/LED/fan: pin), RGB (NeoPixel strip: pin + count), SERVO (pin),
//       BUZZER (pin), SENSOR (analog pin, or PIN_CHIP_TEMP for the ESP32's own temperature)
// id is what AMO calls it ("turn on the fan"); name is optional ("desk light").
enum PartType { SWITCH, RGB, SERVO, BUZZER, SENSOR };
#define PIN_CHIP_TEMP -1

struct PartSpec {
  const char* id;
  const char* name;
  PartType type;
  int pin;
  int count;        // RGB: LEDs in the strip.  SENSOR: unused
  const char* unit; // SENSOR: "°C", "%", …
  float scale;      // SENSOR: reading = analogRead(pin) * scale + offset
  float offset;
};

// Pins below suit an ESP32 DevKit. On an ESP32-S3 avoid GPIO 26–32 (flash) — use e.g. 4, 5, 6, 7, 15.
static PartSpec PARTS[] = {
  // id       name          type    pin  count unit   scale  offset
  {"fan",    "fan",        SWITCH, 26,  0,    "",    0,     0},
  {"light",  "desk light", RGB,    27,  12,   "",    0,     0},
  {"head",   "head",       SERVO,  13,  0,    "",    0,     0},
  {"beeper", "beeper",     BUZZER, 25,  0,    "",    0,     0},
  {"temp",   "temperature",SENSOR, PIN_CHIP_TEMP, 0, "°C", 1, 0},
};
