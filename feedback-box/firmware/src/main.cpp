// Feedback box (C&G #213) for the ESP32-WROOM-32 DevKit.
//
// Three buttons (happy / ok / sad) to GND on GPIO 25 / 26 / 27, internal pull-ups.
// The BOOT button (GPIO 0) sends a "test" press, so the whole path can be tried before
// the real buttons arrive; held 5 s it clears the Wi-Fi settings and reopens setup.
// The onboard LED (GPIO 2): one long flash = thank you; slow blink = setup mode;
// three quick flashes every few seconds = the server refuses this box's key.
//
// Each press gets a number (seq) kept across restarts and goes into a queue stored in
// flash (64 presses). A separate task sends the queue over Wi-Fi, oldest first; a press
// the server already has (seq seen) counts once. No Wi-Fi password, server address or
// key is in this code: they are typed into the setup page the box opens on its own Wi-Fi.

#include <Arduino.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>
#include "bootloader_random.h"

extern const uint8_t rootca_crt_bundle_start[] asm("_binary_data_cert_x509_crt_bundle_bin_start");

// ---- pins and timings ---------------------------------------------------------------
static const uint8_t PIN_HAPPY = 25, PIN_OK = 26, PIN_SAD = 27, PIN_BOOT = 0, PIN_LED = 2;
static const uint32_t DEBOUNCE_MS = 30;
static const uint32_t PRESS_GAP_MS = 3000;   // one press per 3 s for the whole box
static const uint32_t RESET_HOLD_MS = 5000;  // BOOT held this long: clear settings
static const uint32_t QUEUE_SLOTS = 64;

enum Rating : uint8_t { RATE_HAPPY = 0, RATE_OK = 1, RATE_SAD = 2, RATE_TEST = 3 };
static const char *RATING_NAME[] = {"happy", "ok", "sad", "test"};

struct Entry {
  uint32_t seq;
  uint32_t pressedMs;  // millis() at the press
  uint32_t bootId;     // which power-on it happened in (age is only known in the same one)
  uint8_t rating;
};

struct Button {
  uint8_t pin;
  Rating rating;
  bool stable;     // true = released (pull-up)
  bool lastRead;
  uint32_t changedMs;
  uint32_t downSinceMs;
};

static Button buttons[] = {
    {PIN_HAPPY, RATE_HAPPY, true, true, 0, 0},
    {PIN_OK, RATE_OK, true, true, 0, 0},
    {PIN_SAD, RATE_SAD, true, true, 0, 0},
    {PIN_BOOT, RATE_TEST, true, true, 0, 0},
};

static Preferences qPrefs;    // namespace "fbq": h, t, seq, boot, e0..e63
static Preferences cfgPrefs;  // namespace "fbcfg": url, key
static SemaphoreHandle_t qLock;
static uint32_t qHead = 0, qTail = 0, nextSeq = 1, bootId = 0;
static String cfgUrl, cfgKey;

static WiFiManager wm;
static WiFiManagerParameter pUrl("url", "Server address (https://...)", "", 120);
static WiFiManagerParameter pKey("key", "Box key", "", 80);

static volatile bool keyRefused = false;
static uint32_t lastAcceptedMs = 0;
static uint32_t thanksUntilMs = 0;
static volatile bool portalOpen = false;

// ---- queue in flash -------------------------------------------------------------------
static String slotKey(uint32_t i) { return "e" + String(i % QUEUE_SLOTS); }

static void queuePush(Rating r) {
  xSemaphoreTake(qLock, portMAX_DELAY);
  Entry e{nextSeq++, millis(), bootId, (uint8_t)r};
  if (qTail - qHead >= QUEUE_SLOTS) qHead++;  // full: drop the oldest
  qPrefs.putBytes(slotKey(qTail).c_str(), &e, sizeof(e));
  qTail++;
  qPrefs.putUInt("t", qTail);
  qPrefs.putUInt("h", qHead);
  qPrefs.putUInt("seq", nextSeq);
  xSemaphoreGive(qLock);
  Serial.printf("press %s seq %u queued (%u waiting)\n", RATING_NAME[r], e.seq, qTail - qHead);
}

static bool queuePeek(Entry &e) {
  xSemaphoreTake(qLock, portMAX_DELAY);
  bool ok = qTail != qHead && qPrefs.getBytes(slotKey(qHead).c_str(), &e, sizeof(e)) == sizeof(e);
  xSemaphoreGive(qLock);
  return ok;
}

static void queuePop(uint32_t seq) {
  xSemaphoreTake(qLock, portMAX_DELAY);
  Entry e;
  // pop only if the head is still the entry that was sent (the push may have dropped it when full)
  if (qTail != qHead && qPrefs.getBytes(slotKey(qHead).c_str(), &e, sizeof(e)) == sizeof(e) && e.seq == seq) {
    qHead++;
    qPrefs.putUInt("h", qHead);
  }
  xSemaphoreGive(qLock);
}

// ---- sending --------------------------------------------------------------------------
// returns the HTTP status, or a negative HTTPClient error
static int sendOne(const Entry &e) {
  char body[96];
  if (e.bootId == bootId) {
    snprintf(body, sizeof(body), "{\"rating\":\"%s\",\"seq\":%u,\"age_s\":%u}", RATING_NAME[e.rating], e.seq,
             (millis() - e.pressedMs) / 1000);
  } else {
    snprintf(body, sizeof(body), "{\"rating\":\"%s\",\"seq\":%u}", RATING_NAME[e.rating], e.seq);
  }
  String url = cfgUrl;
  if (url.endsWith("/")) url.remove(url.length() - 1);
  url += "/api/rating";

  HTTPClient http;
  http.setTimeout(8000);
  int code;
  if (url.startsWith("https://")) {
    WiFiClientSecure tls;
    tls.setCACertBundle(rootca_crt_bundle_start);
    if (!http.begin(tls, url)) return -100;
    http.addHeader("content-type", "application/json");
    http.addHeader("authorization", "Bearer " + cfgKey);
    code = http.POST((uint8_t *)body, strlen(body));
    http.end();
  } else {
    WiFiClient plain;  // http:// only for a test server on the same network
    if (!http.begin(plain, url)) return -100;
    http.addHeader("content-type", "application/json");
    http.addHeader("authorization", "Bearer " + cfgKey);
    code = http.POST((uint8_t *)body, strlen(body));
    http.end();
  }
  return code;
}

static void senderTask(void *) {
  uint32_t backoffMs = 2000;
  for (;;) {
    Entry e;
    // settings are only read here while setup is closed (the setup page writes them)
    if (portalOpen || cfgUrl.isEmpty() || cfgKey.isEmpty() || WiFi.status() != WL_CONNECTED || !queuePeek(e)) {
      vTaskDelay(pdMS_TO_TICKS(1000));
      continue;
    }
    int code = sendOne(e);
    Serial.printf("send seq %u -> %d\n", e.seq, code);
    if (code == 200 || code == 201) {
      queuePop(e.seq);
      keyRefused = false;
      backoffMs = 2000;
      vTaskDelay(pdMS_TO_TICKS(2100));  // the server takes one press per 2 s from a box
    } else if (code == 400) {
      queuePop(e.seq);  // the server will never take this one; do not block the rest
    } else if (code == 429) {
      vTaskDelay(pdMS_TO_TICKS(3000));
    } else if (code == 401) {
      keyRefused = true;
      vTaskDelay(pdMS_TO_TICKS(60000));
    } else {
      vTaskDelay(pdMS_TO_TICKS(backoffMs));
      backoffMs = min<uint32_t>(backoffMs * 2, 60000);
    }
  }
}

// ---- setup portal ---------------------------------------------------------------------
static void saveParams() {
  cfgUrl = String(pUrl.getValue());
  cfgKey = String(pKey.getValue());
  cfgUrl.trim();
  cfgKey.trim();
  cfgPrefs.putString("url", cfgUrl);
  cfgPrefs.putString("key", cfgKey);
  Serial.printf("settings saved: server %s, key %s\n", cfgUrl.c_str(), cfgKey.isEmpty() ? "missing" : "set");
}

static void clearAndRestart() {
  Serial.println("BOOT held 5 s: clearing Wi-Fi and box settings");
  for (int i = 0; i < 10; i++) {
    digitalWrite(PIN_LED, i % 2);
    delay(100);
  }
  wm.resetSettings();
  cfgPrefs.clear();
  ESP.restart();
}

// ---- buttons --------------------------------------------------------------------------
static void readButtons(uint32_t now) {
  for (auto &b : buttons) {
    bool r = digitalRead(b.pin);
    if (r != b.lastRead) {
      b.lastRead = r;
      b.changedMs = now;
    }
    if (now - b.changedMs < DEBOUNCE_MS || r == b.stable) continue;
    b.stable = r;
    if (!r) {  // pressed
      b.downSinceMs = now;
      if (now - lastAcceptedMs >= PRESS_GAP_MS || lastAcceptedMs == 0) {
        lastAcceptedMs = now;
        queuePush(b.rating);
        thanksUntilMs = now + 600;
      } else {
        Serial.println("press ignored (3 s gap)");
      }
    }
  }
  Button &boot = buttons[3];
  if (!boot.stable && now - boot.downSinceMs >= RESET_HOLD_MS) clearAndRestart();
}

static void driveLed(uint32_t now) {
  bool on;
  if ((int32_t)(thanksUntilMs - now) > 0) on = true;
  else if (portalOpen) on = (now / 500) % 2;
  else if (keyRefused) { uint32_t p = now % 4000; on = p < 600 && (p / 100) % 2 == 0; }
  else on = false;
  digitalWrite(PIN_LED, on);
}

// ---- main -----------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  for (auto &b : buttons) pinMode(b.pin, INPUT_PULLUP);

  qLock = xSemaphoreCreateMutex();
  qPrefs.begin("fbq", false);
  cfgPrefs.begin("fbcfg", false);
  qHead = qPrefs.getUInt("h", 0);
  qTail = qPrefs.getUInt("t", 0);
  // A box that was erase-flashed has no "seq": start at a random number, not at 1, so the
  // server does not take its first presses as repeats of the ones it stored before (C&G #213).
  if (qPrefs.isKey("seq")) {
    nextSeq = qPrefs.getUInt("seq", 1);
  } else {
    // Wi-Fi is not on yet, so esp_random() would only be pseudo-random here: switch on the
    // hardware entropy source for this one read (safe while Wi-Fi and the ADC are off).
    bootloader_random_enable();
    nextSeq = (esp_random() & 0x3FFFFFFF) | 1;  // 1 .. 2^30-1; the server takes up to 2^31
    bootloader_random_disable();
    qPrefs.putUInt("seq", nextSeq);
  }
  bootId = qPrefs.getUInt("boot", 0) + 1;
  qPrefs.putUInt("boot", bootId);
  cfgUrl = cfgPrefs.getString("url", "");
  cfgKey = cfgPrefs.getString("key", "");
  Serial.printf("\nfeedback box: boot %u, %u presses waiting, next seq %u\n", bootId, qTail - qHead, nextSeq);

  pUrl.setValue(cfgUrl.c_str(), 120);
  pKey.setValue(cfgKey.c_str(), 80);
  WiFi.mode(WIFI_STA);
  wm.addParameter(&pUrl);
  wm.addParameter(&pKey);
  wm.setSaveParamsCallback(saveParams);
  wm.setConfigPortalBlocking(false);  // buttons keep working while setup is open
  wm.setConnectTimeout(20);
  wm.setTitle("Feedback box");

  String ap = "Feedback-Box-" + String((uint32_t)ESP.getEfuseMac() & 0xFFFF, HEX);
  bool connected = wm.autoConnect(ap.c_str());  // opens the setup Wi-Fi if no network is saved or it fails
  portalOpen = !connected;
  if (connected && (cfgUrl.isEmpty() || cfgKey.isEmpty())) {
    wm.startConfigPortal(ap.c_str());  // on Wi-Fi but the server or key is missing
    portalOpen = true;
  }
  Serial.println(portalOpen ? "setup open on Wi-Fi " + ap : "Wi-Fi connected: " + WiFi.localIP().toString());
  xTaskCreatePinnedToCore(senderTask, "sender", 8192, nullptr, 1, nullptr, 0);
}

void loop() {
  uint32_t now = millis();
  if (portalOpen) {
    wm.process();
    if (WiFi.status() == WL_CONNECTED && !cfgUrl.isEmpty() && !cfgKey.isEmpty()) {
      wm.stopConfigPortal();
      portalOpen = false;
      Serial.println("setup closed, Wi-Fi connected: " + WiFi.localIP().toString());
    }
  }
  readButtons(now);
  driveLed(now);
  delay(5);
}
