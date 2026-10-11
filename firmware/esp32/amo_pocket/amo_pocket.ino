// AMO Pocket — AMO's pet body: XIAO ESP32S3 Sense + 1.28" round display + speaker + 2 buttons.
//
// Online (Wi-Fi + the brain on your Mac reachable):
//   hold TALK  → talk to AMO, he answers through the speaker (sentence by sentence, as he thinks)
//   tap TALK   → the time · press LOOK → "what do you see?" · hold LOOK 2 s → today's brief
//   his face shows his mood, listens, thinks, talks (word by word), and your next timer
// Offline (no Wi-Fi / brain unreachable) — he keeps working from his microSD card:
//   shows today's sessions, goals and timers (copied while online), rings your timers and reminders,
//   tells the time, plays today's brief, and RECORDS voice notes and photos, which are sent to the
//   brain when he's back (AMO then saves them and asks before doing anything you asked for).
//
// Libraries: ArduinoJson (7), GFX Library for Arduino (Arduino_GFX). Board: "XIAO_ESP32S3",
// PSRAM: "OPI PSRAM" (required). Edit config.h, then Upload. Brain side: amo/pocket.py.

#include <WiFi.h>
#include <WiFiMulti.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <SD.h>
#include <ESP_I2S.h>
#include <Arduino_GFX_Library.h>
#include <sys/time.h>
#include <time.h>
#include "esp_camera.h"
#include "config.h"

// ------------------------------------------------------------------ hardware
Arduino_DataBus *bus = new Arduino_HWSPI(PIN_LCD_DC, PIN_LCD_CS, PIN_SCK, PIN_MOSI, PIN_MISO, &SPI, true);
Arduino_GFX *lcd = new Arduino_GC9A01(bus, GFX_NOT_DEFINED, 0, true);
Arduino_Canvas *gfx = new Arduino_Canvas(240, 240, lcd);  // drawn off-screen, then flushed: no flicker
I2SClass mic, spk;
WiFiMulti wifi;
SemaphoreHandle_t spiLock;   // display and microSD share the SPI bus
SemaphoreHandle_t spkLock;   // one sound at a time
bool sdOk = false, camOk = false;

// ------------------------------------------------------------------ state
volatile bool online = false, busy = false, stopPlayback = false;
struct FaceNow { char state[12] = "idle"; char mood[10] = "chill"; char word[24] = ""; bool speaking = false; float level = 0; };
FaceNow faceNow;
SemaphoreHandle_t faceLock;
String localMode = "";          // "listening" | "thinking" | "happy" | "" (brain decides)
uint32_t localModeUntil = 0;
String banner = "";             // a short line under the face ("Pasta done!")
uint32_t bannerUntil = 0;

struct Timer { int id; time_t due; char label[20]; char clip[12]; };
Timer timers[16];
int timerCount = 0;
struct Goal { char title[42]; bool done; };
Goal goals[8];
int goalCount = 0;
char nextSession[40] = "";
char briefClip[24] = "";
long tzOffset = 0;
long tzChange = 0, tzOffsetAfter = 0;  // daylight saving: the offset switches at tzChange (epoch)

long localOffset(time_t t) { return (tzChange && t >= tzChange) ? tzOffsetAfter : tzOffset; }

// ------------------------------------------------------------------ small helpers
String url(const String &path) { return String(AMO_BRAIN) + path; }

void addAuth(HTTPClient &http) {
  if (strlen(AMO_KEY)) http.addHeader("Authorization", String("Bearer ") + AMO_KEY);
}

bool timeKnown() { return time(nullptr) > 1700000000; }

String newId() {
  char b[24];
  snprintf(b, sizeof b, "%08lx%08lx", (unsigned long)esp_random(), (unsigned long)(timeKnown() ? time(nullptr) : millis()));
  return String(b);
}

String urlEncode(const String &s) {
  String out;
  const char *hex = "0123456789ABCDEF";
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (isalnum((unsigned char)c) || c == '-' || c == '_' || c == '.' || c == '~') out += c;
    else { out += '%'; out += hex[(c >> 4) & 15]; out += hex[c & 15]; }
  }
  return out;
}

void setBanner(const String &text, uint32_t ms) { banner = text; bannerUntil = millis() + ms; }
void setLocal(const String &mode, uint32_t ms) { localMode = mode; localModeUntil = millis() + ms; }

// ------------------------------------------------------------------ sound
void playPcm(const uint8_t *data, size_t len) {
  static int16_t buf[512];
  size_t samples = len / 2;
  const int16_t *in = (const int16_t *)data;
  for (size_t i = 0; i < samples && !stopPlayback; i += 512) {
    size_t n = min((size_t)512, samples - i);
    for (size_t k = 0; k < n; k++) buf[k] = (int16_t)(in[i + k] * SPEAKER_VOLUME);
    spk.write((uint8_t *)buf, n * 2);
  }
}

void chime() {
  static int16_t tone[1600];
  xSemaphoreTake(spkLock, portMAX_DELAY);
  for (int note = 0; note < 2; note++) {
    float f = note ? 1318.5f : 987.8f;
    for (int i = 0; i < 1600; i++) tone[i] = (int16_t)(sinf(2 * PI * f * i / 16000.0f) * 9000 * (1.0f - i / 1600.0f));
    playPcm((uint8_t *)tone, sizeof tone);
  }
  xSemaphoreGive(spkLock);
}

// Play a clip from the microSD card (/amo/clips/<id>.wav, 16 kHz mono). False if missing.
bool playClip(const String &id) {
  if (!sdOk || id.isEmpty()) return false;
  static uint8_t buf[2048];
  xSemaphoreTake(spiLock, portMAX_DELAY);
  File f = SD.open("/amo/clips/" + id + ".wav");
  if (!f || f.size() < 44) { if (f) f.close(); xSemaphoreGive(spiLock); return false; }
  f.seek(44);
  xSemaphoreGive(spiLock);
  xSemaphoreTake(spkLock, portMAX_DELAY);
  while (!stopPlayback) {
    xSemaphoreTake(spiLock, portMAX_DELAY);
    int n = f.read(buf, sizeof buf);
    xSemaphoreGive(spiLock);
    if (n <= 0) break;
    playPcm(buf, n & ~1);
  }
  xSemaphoreTake(spiLock, portMAX_DELAY);
  f.close();
  xSemaphoreGive(spiLock);
  xSemaphoreGive(spkLock);
  return true;
}

void sayTime() {
  if (!timeKnown()) { playClip("sleepy"); return; }
  time_t now = time(nullptr); now += localOffset(now);
  struct tm t; gmtime_r(&now, &t);
  int h = t.tm_hour % 12; if (!h) h = 12;
  char m[6]; snprintf(m, sizeof m, "m%02d", t.tm_min);
  char hh[6]; snprintf(hh, sizeof hh, "h%d", h);
  playClip("its"); playClip(hh); playClip(m); playClip(t.tm_hour < 12 ? "am" : "pm");
}

// Streams AMO's spoken reply (raw 16 kHz PCM) straight to the speaker as it arrives.
class SpeakerStream : public Stream {
  int carry = -1;
 public:
  size_t write(const uint8_t *b, size_t n) override {
    if (stopPlayback) return 0;
    static uint8_t tmp[2050];
    size_t done = 0;
    while (done < n) {
      size_t k = 0;
      if (carry >= 0) { tmp[k++] = (uint8_t)carry; carry = -1; }
      size_t take = min(n - done, sizeof(tmp) - k);
      memcpy(tmp + k, b + done, take);
      k += take; done += take;
      if (k & 1) { carry = tmp[--k]; }
      playPcm(tmp, k);
    }
    return n;
  }
  size_t write(uint8_t c) override { return write(&c, 1); }
  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }
  void flush() override {}
};

// ------------------------------------------------------------------ microphone
uint8_t *recordWhileHeld(int pin, size_t *outLen) {
  const size_t maxBytes = 16000 * 2 * MAX_RECORD_SECONDS;
  uint8_t *pcm = (uint8_t *)ps_malloc(maxBytes);
  if (!pcm) return nullptr;
  size_t len = 0;
  while (digitalRead(pin) == LOW && len < maxBytes) {
    size_t n = mic.readBytes((char *)pcm + len, min((size_t)1024, maxBytes - len));
    int16_t *s = (int16_t *)(pcm + len);
    for (size_t i = 0; i < n / 2; i++) s[i] = (int16_t)constrain((int32_t)s[i] * MIC_GAIN, -32768, 32767);
    len += n & ~1;
  }
  *outLen = len;
  return pcm;
}

void wavHeader(uint8_t *h, uint32_t dataLen) {
  uint32_t rate = 16000, byteRate = 32000, chunk = 36 + dataLen;
  memcpy(h, "RIFF", 4); memcpy(h + 4, &chunk, 4); memcpy(h + 8, "WAVEfmt ", 8);
  uint32_t fmtLen = 16; uint16_t pcmFmt = 1, ch = 1, align = 2, bits = 16;
  memcpy(h + 16, &fmtLen, 4); memcpy(h + 20, &pcmFmt, 2); memcpy(h + 22, &ch, 2); memcpy(h + 24, &rate, 4);
  memcpy(h + 28, &byteRate, 4); memcpy(h + 32, &align, 2); memcpy(h + 34, &bits, 2);
  memcpy(h + 36, "data", 4); memcpy(h + 40, &dataLen, 4);
}

// ------------------------------------------------------------------ camera
bool cameraBegin() {
  camera_config_t c = {};
  c.pin_pwdn = -1; c.pin_reset = -1; c.pin_xclk = 10;
  c.pin_sccb_sda = 40; c.pin_sccb_scl = 39;
  c.pin_d7 = 48; c.pin_d6 = 11; c.pin_d5 = 12; c.pin_d4 = 14;
  c.pin_d3 = 16; c.pin_d2 = 18; c.pin_d1 = 17; c.pin_d0 = 15;
  c.pin_vsync = 38; c.pin_href = 47; c.pin_pclk = 13;
  c.xclk_freq_hz = 20000000; c.ledc_timer = LEDC_TIMER_0; c.ledc_channel = LEDC_CHANNEL_0;
  c.pixel_format = PIXFORMAT_JPEG; c.frame_size = FRAMESIZE_VGA; c.jpeg_quality = 12;
  c.fb_count = 1; c.fb_location = CAMERA_FB_IN_PSRAM; c.grab_mode = CAMERA_GRAB_LATEST;
  return esp_camera_init(&c) == ESP_OK;
}

// ------------------------------------------------------------------ the offline journal (microSD)
void journal(const char *type, const uint8_t *data, size_t len, const char *ext, const uint8_t *head = nullptr, size_t headLen = 0) {
  if (!sdOk) return;
  String id = newId();
  xSemaphoreTake(spiLock, portMAX_DELAY);
  if (data && len) {
    File f = SD.open("/amo/journal/" + id + "." + ext, FILE_WRITE);
    if (f) { if (head) f.write(head, headLen); f.write(data, len); f.close(); }
  }
  File m = SD.open("/amo/journal/" + id + ".json", FILE_WRITE);
  if (m) {
    m.printf("{\"id\":\"%s\",\"type\":\"%s\",\"at\":%ld,\"file\":\"%s\"}", id.c_str(), type,
             timeKnown() ? (long)time(nullptr) : 0L, (data && len) ? (id + "." + ext).c_str() : "");
    m.close();
  }
  xSemaphoreGive(spiLock);
}

// ------------------------------------------------------------------ talking to the brain
bool postStream(const String &path, const uint8_t *body, size_t len, const char *type) {
  HTTPClient http;
  http.setTimeout(120000);
  http.begin(url(path));
  addAuth(http);
  http.addHeader("Content-Type", type);
  int code = http.POST((uint8_t *)body, len);
  if (code != 200) { http.end(); return false; }
  SpeakerStream out;
  xSemaphoreTake(spkLock, portMAX_DELAY);
  http.writeToStream(&out);
  xSemaphoreGive(spkLock);
  http.end();
  return true;
}

void talk() {
  setLocal("listening", 60000);
  size_t len = 0;
  uint32_t started = millis();
  uint8_t *pcm = recordWhileHeld(PIN_BTN_TALK, &len);
  if (millis() - started < 400) {  // a tap, not a hold: tell the time
    if (pcm) free(pcm);
    setLocal("", 0);
    sayTime();
    return;
  }
  if (!pcm || len < 16000) { if (pcm) free(pcm); setLocal("", 0); return; }
  uint8_t head[44];
  wavHeader(head, len);
  if (online) {
    uint8_t *wav = (uint8_t *)ps_malloc(len + 44);
    if (wav) {
      memcpy(wav, head, 44); memcpy(wav + 44, pcm, len);
      setLocal("thinking", 60000);
      busy = true;
      bool ok = postStream("/api/pocket/talk", wav, len + 44, "audio/wav");
      busy = false;
      free(wav);
      setLocal("", 0);
      if (ok) { free(pcm); return; }
    }
  }
  // offline (or the brain didn't answer): keep it for later
  journal("voice_note", pcm, len, "wav", head, 44);
  free(pcm);
  setLocal("", 0);
  setBanner("Saved for later", 3000);
  if (!playClip("offline")) chime();
}

void look() {
  uint32_t started = millis();
  while (digitalRead(PIN_BTN_LOOK) == LOW) {
    if (millis() - started > 2000) {  // held: today's brief
      setBanner("Today's brief", 4000);
      if (!playClip(briefClip)) chime();
      while (digitalRead(PIN_BTN_LOOK) == LOW) delay(10);
      return;
    }
    delay(10);
  }
  if (!camOk) { chime(); return; }
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) { chime(); return; }
  setLocal("thinking", 60000);
  bool ok = false;
  if (online) {
    busy = true;
    ok = postStream("/api/pocket/look", fb->buf, fb->len, "image/jpeg");
    busy = false;
  }
  if (!ok) {
    journal("photo", fb->buf, fb->len, "jpg");
    setBanner("Photo saved", 3000);
    if (!playClip("photo")) chime();
  }
  esp_camera_fb_return(fb);
  setLocal("", 0);
}

// ------------------------------------------------------------------ syncing (core 0)
bool getJson(const String &path, JsonDocument &doc, int timeout = 8000) {
  HTTPClient http;
  http.setTimeout(timeout);
  http.begin(url(path));
  addAuth(http);
  int code = http.GET();
  bool ok = code == 200 && !deserializeJson(doc, http.getStream());
  http.end();
  return ok;
}

bool download(const String &path, const String &file) {
  HTTPClient http;
  http.setTimeout(30000);
  http.begin(url(path));
  addAuth(http);
  if (http.GET() != 200) { http.end(); return false; }
  int total = http.getSize();
  uint8_t *buf = (uint8_t *)ps_malloc(total > 0 ? total : 400000);
  if (!buf) { http.end(); return false; }
  WiFiClient *s = http.getStreamPtr();
  int got = 0, cap = total > 0 ? total : 400000;
  uint32_t t0 = millis();
  while (http.connected() && got < cap && millis() - t0 < 30000) {
    int n = s->readBytes(buf + got, min(4096, cap - got));
    if (n <= 0) { if (total > 0 && got >= total) break; delay(5); continue; }
    got += n;
    if (total > 0 && got >= total) break;
  }
  http.end();
  bool ok = got > 44;
  if (ok) {
    xSemaphoreTake(spiLock, portMAX_DELAY);
    File f = SD.open(file, FILE_WRITE);
    if (f) { f.write(buf, got); f.close(); } else ok = false;
    xSemaphoreGive(spiLock);
  }
  free(buf);
  return ok;
}

String readSmall(const String &file) {
  xSemaphoreTake(spiLock, portMAX_DELAY);
  File f = SD.open(file);
  String s = f ? f.readString() : "";
  if (f) f.close();
  xSemaphoreGive(spiLock);
  return s;
}

void writeSmall(const String &file, const String &text) {
  xSemaphoreTake(spiLock, portMAX_DELAY);
  File f = SD.open(file, FILE_WRITE);
  if (f) { f.print(text); f.close(); }
  xSemaphoreGive(spiLock);
}

bool fired(int id) { return readSmall("/amo/fired.txt").indexOf("," + String(id) + ",") >= 0; }
void markFired(int id) {
  String s = readSmall("/amo/fired.txt");
  if (s.length() > 2000) s = s.substring(s.length() - 1000);
  writeSmall("/amo/fired.txt", (s.isEmpty() ? String(",") : s) + String(id) + ",");
}

void applySnapshot(JsonDocument &snap) {
  if (snap["now"].as<long>() > 1700000000) {
    struct timeval tv = {(time_t)snap["now"].as<long>(), 0};
    settimeofday(&tv, nullptr);
  }
  tzOffset = snap["tz_offset"] | 0L;
  tzChange = snap["tz_change"] | 0L;
  tzOffsetAfter = snap["tz_offset_after"] | tzOffset;
  timerCount = 0;
  for (JsonObject t : snap["timers"].as<JsonArray>()) {
    if (timerCount >= 16) break;
    Timer &x = timers[timerCount++];
    x.id = t["id"]; x.due = t["due"].as<long>();
    strlcpy(x.label, t["label"] | "Timer", sizeof x.label);
    strlcpy(x.clip, t["clip"] | "", sizeof x.clip);
  }
  goalCount = 0;
  for (JsonObject g : snap["goals"].as<JsonArray>()) {
    if (goalCount >= 8) break;
    strlcpy(goals[goalCount].title, g["title"] | "", sizeof goals[0].title);
    goals[goalCount++].done = g["done"] | false;
  }
  nextSession[0] = 0;
  JsonArray ses = snap["sessions"].as<JsonArray>();
  if (ses.size()) snprintf(nextSession, sizeof nextSession, "%s %s", ses[0]["time"] | "", ses[0]["who"] | "");
  strlcpy(briefClip, snap["brief"] | "", sizeof briefClip);
  xSemaphoreTake(faceLock, portMAX_DELAY);
  strlcpy(faceNow.mood, snap["mood"] | "chill", sizeof faceNow.mood);
  xSemaphoreGive(faceLock);
}

void uploadJournal() {
  xSemaphoreTake(spiLock, portMAX_DELAY);
  File dir = SD.open("/amo/journal");
  String metas[40]; int n = 0;
  for (File f = dir.openNextFile(); f && n < 40; f = dir.openNextFile()) {
    String name = f.name();
    if (name.endsWith(".json")) metas[n++] = name;
    f.close();
  }
  dir.close();
  xSemaphoreGive(spiLock);
  if (!n) return;
  for (int i = 0; i < n && online; i++) {
    String meta = readSmall("/amo/journal/" + metas[i]);
    JsonDocument doc;
    if (deserializeJson(doc, meta)) {  // a half-written entry (battery died?) — drop it
      xSemaphoreTake(spiLock, portMAX_DELAY);
      SD.remove("/amo/journal/" + metas[i]);
      xSemaphoreGive(spiLock);
      continue;
    }
    String file = doc["file"] | "";
    uint8_t *body = nullptr; size_t len = 0;
    if (file.length()) {
      xSemaphoreTake(spiLock, portMAX_DELAY);
      File f = SD.open("/amo/journal/" + file);
      if (f) { len = f.size(); body = (uint8_t *)ps_malloc(len); if (body) f.read(body, len); f.close(); }
      xSemaphoreGive(spiLock);
    }
    doc.remove("file");
    String ev; serializeJson(doc, ev);
    HTTPClient http;
    http.setTimeout(30000);
    http.begin(url("/api/pocket/sync?event=" + urlEncode(ev)));
    addAuth(http);
    int code = body ? http.POST(body, len) : http.POST((uint8_t *)"", 0);
    http.end();
    if (body) free(body);
    if (code == 200) {
      xSemaphoreTake(spiLock, portMAX_DELAY);
      SD.remove("/amo/journal/" + metas[i]);
      if (file.length()) SD.remove("/amo/journal/" + file);
      xSemaphoreGive(spiLock);
    }
  }
  JsonDocument done;
  HTTPClient http;
  http.setTimeout(180000);  // the brain transcribes the voice notes now
  http.begin(url("/api/pocket/synced"));
  addAuth(http);
  if (http.POST((uint8_t *)"", 0) == 200 && !deserializeJson(done, http.getStream())) {
    String say = done["say"] | "";
    http.end();
    if (say.length() && download("/api/pocket/clip/" + say, "/amo/clips/" + say + ".wav")) {
      while (busy) delay(200);
      playClip(say);
    }
    return;
  }
  http.end();
}

void syncNow() {
  JsonDocument health;
  bool was = online;
  online = WiFi.status() == WL_CONNECTED && getJson("/api/health", health, 4000);
  if (!online) return;
  if (!was) setBanner("Back online", 3000);
  if (sdOk) uploadJournal();
  JsonDocument snap;
  if (!getJson("/api/pocket/snapshot", snap, 15000)) return;
  applySnapshot(snap);
  if (!sdOk) return;
  String raw; serializeJson(snap, raw);
  writeSmall("/amo/snapshot.json", raw);
  int fetched = 0;  // keep each round short: a few clips at a time
  for (JsonObject c : snap["clips"].as<JsonArray>()) {
    String id = c["id"] | "", v = c["v"] | "";
    if (id.isEmpty() || readSmall("/amo/clips/" + id + ".v") == v) continue;
    if (busy || fetched >= 12) break;
    if (download("/api/pocket/clip/" + id, "/amo/clips/" + id + ".wav")) {
      writeSmall("/amo/clips/" + id + ".v", v);
      fetched++;
    }
  }
}

void netTask(void *) {
  uint32_t lastSync = 0;
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) { online = false; wifi.run(3000); }
    if (millis() - lastSync > SYNC_EVERY_SECONDS * 1000UL || lastSync == 0 || (!online && millis() - lastSync > 15000)) {
      if (!busy) { syncNow(); lastSync = millis(); }
    }
    if (online) {  // what the brain says the face should be doing right now
      JsonDocument f;
      if (getJson("/api/face/now", f, 1500)) {
        xSemaphoreTake(faceLock, portMAX_DELAY);
        strlcpy(faceNow.state, f["state"] | "idle", sizeof faceNow.state);
        strlcpy(faceNow.word, f["word"] | "", sizeof faceNow.word);
        if (f["mood"].as<const char *>()) strlcpy(faceNow.mood, f["mood"], sizeof faceNow.mood);
        faceNow.speaking = f["speaking"] | false;
        faceNow.level = f["level"] | 0.0f;
        xSemaphoreGive(faceLock);
      } else online = false;
    }
    vTaskDelay(pdMS_TO_TICKS(online ? 120 : 1000));
  }
}

// ------------------------------------------------------------------ the face
uint16_t moodColor(const char *mood) {
  if (!strcmp(mood, "hype")) return RGB565(255, 214, 64);
  if (!strcmp(mood, "steady")) return RGB565(120, 170, 255);
  if (!strcmp(mood, "firm")) return RGB565(255, 150, 60);
  if (!strcmp(mood, "quiet")) return RGB565(80, 120, 140);
  return RGB565(94, 231, 255);  // chill
}

void drawFace() {
  FaceNow f;
  xSemaphoreTake(faceLock, portMAX_DELAY);
  f = faceNow;
  xSemaphoreGive(faceLock);
  if (localMode.length() && millis() > localModeUntil) localMode = "";
  String mode = localMode.length() ? localMode : (f.speaking ? String("speaking") : String(f.state));
  if (!online && !localMode.length()) mode = "asleep";
  uint16_t glow = mode == "asleep" ? RGB565(40, 90, 110) : moodColor(f.mood);
  static uint32_t blinkAt = 3000;
  uint32_t t = millis();
  float lid = 1;
  if (t > blinkAt) { float p = (t - blinkAt) / 140.0f; lid = p < 1 ? fabsf(1 - 2 * p) : 1; if (p >= 1) blinkAt = t + 2500 + esp_random() % 4000; }

  gfx->fillScreen(RGB565_BLACK);
  int eyeW = 34, eyeH = mode == "asleep" ? 6 : mode == "listening" ? 66 : mode == "happy" ? 24 : 56;
  eyeH = max(4, (int)(eyeH * max(lid, 0.08f)));
  int up = mode == "thinking" ? -10 : 0, cy = 92 + up;
  for (int x : {82, 158}) {
    if (mode == "happy") {  // ^ ^ eyes
      gfx->fillTriangle(x - 18, cy + 8, x, cy - 10, x + 18, cy + 8, glow);
      gfx->fillTriangle(x - 12, cy + 9, x, cy - 3, x + 12, cy + 9, RGB565_BLACK);
    } else {
      gfx->fillRoundRect(x - eyeW / 2, cy - eyeH / 2, eyeW, eyeH, min(eyeW, eyeH) / 2, glow);
    }
  }
  if (mode == "speaking") {
    int h = 6 + (int)(f.level * 34), w = 70 - (int)(f.level * 16);
    gfx->fillRoundRect(120 - w / 2, 152 - h / 2, w, h, min(w, h) / 2, glow);
  } else if (mode == "asleep") {
    gfx->fillRoundRect(110, 150, 20, 8, 4, glow);
    gfx->setTextColor(glow); gfx->setTextSize(2); gfx->setCursor(170, 50); gfx->print("z");
    gfx->setTextSize(1); gfx->setCursor(188, 40); gfx->print("z");
  } else if (mode == "thinking") {
    gfx->fillRoundRect(100, 150, 40, 6, 3, glow);
  } else if (mode == "listening") {
    gfx->drawCircle(120, 120, 116 - (t / 60) % 8, glow);
    gfx->fillCircle(120, 152, 9, RGB565(255, 80, 80));
  } else {  // calm smile
    gfx->fillArc(120, 128, 34, 29, 30, 150, glow);
  }

  // the line under the face: what he's saying, a banner, your next timer, or the clock
  String line = "";
  if (mode == "speaking" && strlen(f.word)) line = f.word;
  else if (banner.length() && millis() < bannerUntil) line = banner;
  else {
    time_t now = time(nullptr);
    for (int i = 0; i < timerCount; i++) {
      long left = timers[i].due - now;
      if (left > 0 && left < 12 * 3600 && timeKnown()) {
        char b[40];
        if (left < 3600) snprintf(b, sizeof b, "%s %ld:%02ld", timers[i].label, left / 60, left % 60);
        else snprintf(b, sizeof b, "%s %ldh%02ld", timers[i].label, left / 3600, (left % 3600) / 60);
        line = b;
        break;
      }
    }
    if (!line.length() && timeKnown()) {
      time_t lt = now + localOffset(now); struct tm tm; gmtime_r(&lt, &tm);
      char b[16]; int h = tm.tm_hour % 12; if (!h) h = 12;
      snprintf(b, sizeof b, "%d:%02d %s", h, tm.tm_min, tm.tm_hour < 12 ? "AM" : "PM");
      line = b;
    }
  }
  if (line.length()) {
    gfx->setTextSize(2);
    gfx->setTextColor(mode == "speaking" ? RGB565_WHITE : RGB565(150, 180, 190));
    int16_t x1, y1; uint16_t w, h;
    gfx->getTextBounds(line.c_str(), 0, 0, &x1, &y1, &w, &h);
    gfx->setCursor(120 - w / 2, 190);
    gfx->print(line);
  }
  // little status dots: Wi-Fi/brain, open goals
  gfx->fillCircle(120, 222, 4, online ? RGB565(80, 220, 120) : RGB565(120, 60, 60));
  int open = 0; for (int i = 0; i < goalCount; i++) open += !goals[i].done;
  for (int i = 0; i < open && i < 5; i++) gfx->fillCircle(96 - i * 10, 222, 3, glow);

  xSemaphoreTake(spiLock, portMAX_DELAY);
  gfx->flush();
  xSemaphoreGive(spiLock);
}

// ------------------------------------------------------------------ timers (work offline too)
void checkTimers() {
  if (!timeKnown()) return;
  time_t now = time(nullptr);
  for (int i = 0; i < timerCount; i++) {
    Timer &t = timers[i];
    if (t.id && now >= t.due && now - t.due < 3600 && !fired(t.id)) {
      markFired(t.id);
      setBanner(String(t.label) + " done!", 8000);
      setLocal("happy", 2000);
      chime();
      if (!playClip(t.clip)) playClip("timer");
    }
  }
}

// ------------------------------------------------------------------ petting
uint32_t touchBase = 0;
void checkPet() {
  uint32_t v = touchRead(PIN_TOUCH);
  if (!touchBase) { touchBase = v; return; }
  if (v > touchBase * TOUCH_SENSITIVITY) {
    if (localMode != "happy") setLocal("happy", 1500);
  } else {
    touchBase = (touchBase * 31 + v) / 32;  // follows slow changes (humidity, the case)
  }
}

// The face keeps moving while AMO talks (the main loop is busy playing his voice then).
void uiTask(void *) {
  for (;;) {
    checkPet();
    drawFace();
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

// ------------------------------------------------------------------ setup / loop
void setup() {
  Serial.begin(115200);
  spiLock = xSemaphoreCreateMutex();
  spkLock = xSemaphoreCreateMutex();
  faceLock = xSemaphoreCreateMutex();
  pinMode(PIN_BTN_TALK, INPUT_PULLUP);
  pinMode(PIN_BTN_LOOK, INPUT_PULLUP);

  if (!gfx->begin(40000000)) Serial.println("display not found — check the wiring");
  gfx->fillScreen(RGB565_BLACK);
  gfx->flush();
  sdOk = SD.begin(PIN_SD_CS, SPI, 20000000);
  if (sdOk) { SD.mkdir("/amo"); SD.mkdir("/amo/clips"); SD.mkdir("/amo/journal"); }
  else Serial.println("no microSD card — offline memory is off");

  mic.setPinsPdmRx(PIN_MIC_CLK, PIN_MIC_DATA);
  if (!mic.begin(I2S_MODE_PDM_RX, 16000, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) Serial.println("mic failed");
  spk.setPins(PIN_I2S_BCLK, PIN_I2S_LRC, PIN_I2S_DOUT);
  if (!spk.begin(I2S_MODE_STD, 16000, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) Serial.println("speaker failed");
  camOk = cameraBegin();
  if (!camOk) Serial.println("camera failed");

  // last copy of the day, so he's useful even if there's no Wi-Fi right now
  if (sdOk) {
    JsonDocument snap;
    if (!deserializeJson(snap, readSmall("/amo/snapshot.json"))) applySnapshot(snap);
  }
  WiFi.mode(WIFI_STA);
  wifi.addAP(WIFI_SSID, WIFI_PASSWORD);
  if (strlen(WIFI2_SSID)) wifi.addAP(WIFI2_SSID, WIFI2_PASSWORD);
  xTaskCreatePinnedToCore(netTask, "amo-net", 16384, nullptr, 1, nullptr, 0);
  xTaskCreatePinnedToCore(uiTask, "amo-ui", 8192, nullptr, 1, nullptr, 1);
  chime();
}

void loop() {
  static uint32_t lastTick = 0;
  if (digitalRead(PIN_BTN_TALK) == LOW) { stopPlayback = false; talk(); }
  if (digitalRead(PIN_BTN_LOOK) == LOW) { stopPlayback = false; look(); }
  if (millis() - lastTick > 1000) { lastTick = millis(); checkTimers(); }
  delay(5);
}
