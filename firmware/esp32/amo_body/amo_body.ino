// AMO body firmware for ESP32 / ESP32-S3.
//
// Connects to AMO's brain over Wi-Fi and lets AMO control what's wired to this board
// ("turn on the fan", "make the light purple", "turn your head to 120") and read its sensors.
// Optionally draws AMO's face (eyes + talking mouth) on a TFT screen.
//
// Libraries (Arduino IDE → Library Manager): ArduinoJson (v7), ESP32Servo, Adafruit NeoPixel,
// and TFT_eSPI only if you enable AMO_FACE. Board: "ESP32 Dev Module" or "ESP32S3 Dev Module".
// Edit config.h, then Upload. Protocol: see amo/hardware.py.

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include <Adafruit_NeoPixel.h>
#include "config.h"

#ifdef AMO_FACE
#include <TFT_eSPI.h>
TFT_eSPI tft;
#endif

const int NPARTS = sizeof(PARTS) / sizeof(PARTS[0]);
Adafruit_NeoPixel* strips[NPARTS];
Servo* servos[NPARTS];
String values[NPARTS];          // current state of each part, as JSON text
SemaphoreHandle_t stateLock;

// ------------------------------------------------------------------ http helpers
bool request(const char* method, const String& path, const String& body, String& out, int timeoutMs) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  http.setTimeout(timeoutMs);
  http.begin(String(AMO_BRAIN) + path);
  if (strlen(AMO_KEY)) http.addHeader("Authorization", String("Bearer ") + AMO_KEY);
  int code;
  if (strcmp(method, "POST") == 0) {
    http.addHeader("Content-Type", "application/json");
    code = http.POST(body);
  } else {
    code = http.GET();
  }
  out = code > 0 ? http.getString() : "";
  http.end();
  return code == 200;
}

// ------------------------------------------------------------------ parts
const char* typeName(PartType t) {
  switch (t) {
    case SWITCH: return "switch";
    case RGB: return "rgb";
    case SERVO: return "servo";
    case BUZZER: return "buzzer";
    default: return "sensor";
  }
}

void setupParts() {
  for (int i = 0; i < NPARTS; i++) {
    PartSpec& p = PARTS[i];
    strips[i] = nullptr;
    servos[i] = nullptr;
    switch (p.type) {
      case SWITCH: pinMode(p.pin, OUTPUT); digitalWrite(p.pin, LOW); values[i] = "false"; break;
      case RGB:
        strips[i] = new Adafruit_NeoPixel(p.count, p.pin, NEO_GRB + NEO_KHZ800);
        strips[i]->begin(); strips[i]->show(); values[i] = "\"#000000\""; break;
      case SERVO:
        servos[i] = new Servo(); servos[i]->attach(p.pin); servos[i]->write(90); values[i] = "90"; break;
      case BUZZER: pinMode(p.pin, OUTPUT); values[i] = "null"; break;
      case SENSOR: values[i] = "null"; break;
    }
  }
}

float readSensor(PartSpec& p) {
  if (p.pin == PIN_CHIP_TEMP) return temperatureRead() * p.scale + p.offset;
  return analogRead(p.pin) * p.scale + p.offset;
}

uint32_t parseColour(const char* hex) {  // "#rrggbb"
  if (!hex || hex[0] != '#' || strlen(hex) != 7) return 0;
  return strtoul(hex + 1, nullptr, 16);
}

void setStrip(int i, uint32_t rgb, int brightness) {
  Adafruit_NeoPixel* s = strips[i];
  s->setBrightness(map(brightness, 0, 100, 0, 255));
  for (int k = 0; k < PARTS[i].count; k++) s->setPixelColor(k, (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255);
  s->show();
}

// Do one command from AMO. Returns "" if OK, otherwise the error.
String apply(const char* part, const char* action, JsonVariant value) {
  for (int i = 0; i < NPARTS; i++) {
    PartSpec& p = PARTS[i];
    if (strcmp(p.id, part) != 0) continue;
    xSemaphoreTake(stateLock, portMAX_DELAY);
    String err = "";
    switch (p.type) {
      case SWITCH: {
        bool on = strcmp(action, "on") == 0 ? true : strcmp(action, "off") == 0 ? false : values[i] != "true";
        digitalWrite(p.pin, on ? HIGH : LOW);
        values[i] = on ? "true" : "false";
        break;
      }
      case RGB: {
        static uint32_t last[16] = {0};
        uint32_t colour = last[i % 16] ? last[i % 16] : 0xFFFFFF;
        int bright = 100;
        if (strcmp(action, "off") == 0) { setStrip(i, 0, 0); values[i] = "\"#000000\""; break; }
        if (strcmp(action, "set") == 0) colour = parseColour(value.as<const char*>());
        if (strcmp(action, "brightness") == 0) bright = value.as<int>();
        last[i % 16] = colour;
        setStrip(i, colour, bright);
        char buf[12]; snprintf(buf, sizeof buf, "\"#%06lx\"", (unsigned long)colour);
        values[i] = buf;
        break;
      }
      case SERVO: {
        int angle = constrain(value.as<int>(), 0, 180);
        servos[i]->write(angle);
        values[i] = String(angle);
        break;
      }
      case BUZZER: {
        int freq = value.is<int>() ? value.as<int>() : 2000;
        tone(p.pin, freq, 120); delay(180); tone(p.pin, freq, 120);
        break;
      }
      case SENSOR: err = "sensors are read-only"; break;
    }
    xSemaphoreGive(stateLock);
    return err;
  }
  return String("no part called ") + part;
}

String stateJson() {
  String s = "{";
  xSemaphoreTake(stateLock, portMAX_DELAY);
  for (int i = 0; i < NPARTS; i++) {
    String v = PARTS[i].type == SENSOR ? String(readSensor(PARTS[i]), 1) : values[i];
    s += (i ? ",\"" : "\"") + String(PARTS[i].id) + "\":" + v;
  }
  xSemaphoreGive(stateLock);
  return s + "}";
}

bool hello() {
  JsonDocument doc;
  doc["device"] = DEVICE_NAME;
  doc["kind"] = "esp32";
  JsonArray parts = doc["parts"].to<JsonArray>();
  for (int i = 0; i < NPARTS; i++) {
    JsonObject p = parts.add<JsonObject>();
    p["id"] = PARTS[i].id; p["name"] = PARTS[i].name; p["type"] = typeName(PARTS[i].type);
    if (PARTS[i].unit && strlen(PARTS[i].unit)) p["unit"] = PARTS[i].unit;
  }
  String body, out;
  serializeJson(doc, body);
  return request("POST", "/api/hw/hello", body, out, 8000);
}

// ------------------------------------------------------------------ the command loop (core 0)
void commandTask(void*) {
  unsigned long lastReport = 0;
  for (;;) {
    String out;
    if (!request("GET", String("/api/hw/poll?device=") + DEVICE_NAME + "&wait=20", "", out, 26000)) {
      if (out.indexOf("unknown device") >= 0 || WiFi.status() == WL_CONNECTED) hello();
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }
    JsonDocument doc;
    if (deserializeJson(doc, out)) continue;
    JsonArray cmds = doc["commands"];
    String done = "[";
    for (JsonObject c : cmds) {
      String err = apply(c["part"] | "", c["action"] | "set", c["value"]);
      done += (done.length() > 1 ? "," : "") + String("{\"id\":") + (int)c["id"] + ",\"ok\":" +
              (err.length() ? "false,\"error\":\"" + err + "\"" : "true") + "}";
    }
    done += "]";
    if (cmds.size() || millis() - lastReport > 10000) {
      String body = String("{\"device\":\"") + DEVICE_NAME + "\",\"state\":" + stateJson() + ",\"done\":" + done + "}";
      if (!request("POST", "/api/hw/report", body, out, 8000)) hello();  // brain forgot us → introduce again
      lastReport = millis();
    }
  }
}

// ------------------------------------------------------------------ the face (core 1)
#ifdef AMO_FACE
void drawFace(const char* state, bool speaking, float level, const char* word, const char* mood) {
  static String lastKey;
  int W = tft.width(), H = tft.height();
  uint16_t glow = strcmp(state, "asleep") == 0 ? 0x2B0F : 0x5F3F;  // dim teal when asleep
  if (strcmp(mood, "hype") == 0) glow = 0x7FE0;
  else if (strcmp(mood, "steady") == 0) glow = 0x4D7F;
  else if (strcmp(mood, "firm") == 0) glow = 0xFD20;
  int eyeH = strcmp(state, "asleep") == 0 ? 6 : strcmp(state, "listening") == 0 ? H / 3 : H / 4;
  int mouthH = speaking ? 6 + (int)(level * H / 6) : 6;
  String key = String(state) + mood + eyeH + "/" + mouthH + word;
  if (key == lastKey) return;
  lastKey = key;
  tft.fillRect(0, 0, W, H * 3 / 4, TFT_BLACK);
  int eyeW = W / 7, eyeY = H / 3 - eyeH / 2;
  int lookUp = strcmp(state, "thinking") == 0 ? -H / 14 : 0;
  tft.fillRoundRect(W / 3 - eyeW / 2, eyeY + lookUp, eyeW, eyeH, eyeW / 2, glow);
  tft.fillRoundRect(W * 2 / 3 - eyeW / 2, eyeY + lookUp, eyeW, eyeH, eyeW / 2, glow);
  int mouthW = W / 4;
  tft.fillRoundRect(W / 2 - mouthW / 2, H * 5 / 8 - mouthH / 2, mouthW, mouthH, min(mouthH, mouthW) / 2, glow);
  tft.fillRect(0, H * 3 / 4, W, H / 4, TFT_BLACK);
  if (speaking && strlen(word)) {
    tft.setTextDatum(MC_DATUM);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.drawString(word, W / 2, H * 7 / 8, 4);
  }
}

void faceTask(void*) {
  for (;;) {
    String out;
    if (request("GET", "/api/face/now", "", out, 1500)) {
      JsonDocument doc;
      if (!deserializeJson(doc, out))
        drawFace(doc["state"] | "idle", doc["speaking"] | false, doc["level"] | 0.0f, doc["word"] | "",
                 doc["mood"] | "");
    }
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}
#endif

// ------------------------------------------------------------------ setup
void setup() {
  Serial.begin(115200);
  stateLock = xSemaphoreCreateMutex();
  setupParts();
#ifdef AMO_FACE
  tft.init(); tft.setRotation(1); tft.fillScreen(TFT_BLACK);
#endif
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) { delay(400); Serial.print("."); }
  Serial.printf("\nconnected: %s\n", WiFi.localIP().toString().c_str());
  while (!hello()) { Serial.println("can't reach AMO's brain — retrying"); delay(3000); }
  Serial.println("AMO knows this board as \"" DEVICE_NAME "\"");
  xTaskCreatePinnedToCore(commandTask, "amo-commands", 12288, nullptr, 1, nullptr, 0);
#ifdef AMO_FACE
  xTaskCreatePinnedToCore(faceTask, "amo-face", 8192, nullptr, 1, nullptr, 1);
#endif
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) { WiFi.reconnect(); delay(2000); }
  delay(500);
}
