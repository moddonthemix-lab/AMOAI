// ---- AMO Pocket (XIAO ESP32S3 Sense) settings — edit, then Upload ----
#pragma once

#define WIFI_SSID     "YourWiFi"
#define WIFI_PASSWORD "YourPassword"
// Optional second network (e.g. your phone's hotspot) — leave "" if none
#define WIFI2_SSID     ""
#define WIFI2_PASSWORD ""
#define AMO_BRAIN     "http://192.168.1.50:8765"   // `amo lan on` on the Mac prints this (or your Tailscale address)
#define AMO_KEY       ""                           // the brain's AMO_API_KEY

// ---- Pins (XIAO ESP32S3 Sense). Camera, mic and microSD are on the board itself. ----
// D-pin → GPIO: D0=1 D1=2 D2=3 D3=4 D4=5 D5=6 D6=43 D7=44 D8=7 D9=8 D10=9
#define PIN_TOUCH     1    // D0  copper-tape "pet me" pad (capacitive touch, nothing else to wire)
#define PIN_LCD_CS    2    // D1  GC9A01 round display CS
#define PIN_LCD_DC    3    // D2  GC9A01 DC
#define PIN_I2S_BCLK  4    // D3  MAX98357A BCLK
#define PIN_I2S_LRC   5    // D4  MAX98357A LRC
#define PIN_I2S_DOUT  6    // D5  MAX98357A DIN
#define PIN_BTN_TALK  43   // D6  button to GND: hold to talk (tap = the time)
#define PIN_BTN_LOOK  44   // D7  button to GND: look (hold 2 s = today's brief)
#define PIN_SCK       7    // D8  SPI clock (display + microSD)
#define PIN_MISO      8    // D9  SPI MISO (microSD)
#define PIN_MOSI      9    // D10 SPI MOSI (display + microSD)
#define PIN_SD_CS     21   // microSD on the Sense board (fixed)
#define PIN_MIC_CLK   42   // PDM mic on the Sense board (fixed)
#define PIN_MIC_DATA  41

// ---- Tuning ----
#define MIC_GAIN        6     // the PDM mic is quiet; raise if AMO says "didn't catch that"
#define SPEAKER_VOLUME  0.8f  // 0..1
#define TOUCH_SENSITIVITY 1.06f  // touched when reading rises 6% over the resting value
#define MAX_RECORD_SECONDS 15
#define SYNC_EVERY_SECONDS 60
