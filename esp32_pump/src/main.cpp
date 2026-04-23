#include <Arduino.h>
#include <Preferences.h>
#include <math.h>

namespace cfg {
constexpr uint32_t SERIAL_BAUD = 115200;

// Adjust these pins to your wiring.
constexpr int HX711_DATA_PIN = 34;  // DOUT
constexpr int HX711_CLOCK_PIN = 27; // SCK

constexpr size_t PUMP_COUNT = 4;
constexpr int PUMP_PINS[PUMP_COUNT] = {16, 17, 18, 19};
constexpr bool PUMP_ACTIVE_LOW = false; // L298N input is usually active-high

constexpr uint32_t HX711_TIMEOUT_MS = 180;
constexpr int DEFAULT_SCALE_READS = 8;
constexpr int DEFAULT_TARE_READS = 12;

constexpr uint32_t FILL_POLL_MS = 120;
constexpr uint32_t FILL_STALL_MS = 3000;
constexpr float FILL_STALL_DELTA_G = 0.15f;
constexpr uint32_t FILL_BASE_TIMEOUT_MS = 20000;
constexpr uint32_t FILL_TIMEOUT_PER_G_MS = 2500;
constexpr uint32_t FILL_DRAIN_WAIT_MS = 350;
} // namespace cfg

class HX711 {
public:
  HX711(int dataPin, int clockPin) : _dataPin(dataPin), _clockPin(clockPin) {}

  void begin() {
    pinMode(_dataPin, INPUT);
    pinMode(_clockPin, OUTPUT);
    digitalWrite(_clockPin, LOW);
  }

  bool isReady() const { return digitalRead(_dataPin) == LOW; }

  bool read(long &raw, uint32_t timeoutMs) {
    const uint32_t start = millis();
    while (!isReady()) {
      if (millis() - start > timeoutMs) {
        return false;
      }
      delay(1);
    }

    unsigned long value = 0;
    noInterrupts();
    for (int i = 0; i < 24; ++i) {
      digitalWrite(_clockPin, HIGH);
      delayMicroseconds(1);
      value = (value << 1) | (digitalRead(_dataPin) ? 1UL : 0UL);
      digitalWrite(_clockPin, LOW);
      delayMicroseconds(1);
    }

    // One extra pulse => gain 128 on next read.
    digitalWrite(_clockPin, HIGH);
    delayMicroseconds(1);
    digitalWrite(_clockPin, LOW);
    interrupts();

    if (value & 0x800000UL) {
      value |= 0xFF000000UL;
    }
    raw = static_cast<long>(value);
    return true;
  }

private:
  int _dataPin;
  int _clockPin;
};

class Scale {
public:
  explicit Scale(HX711 &hx711) : _hx711(hx711) {}

  void begin() {
    _hx711.begin();
    _prefs.begin("barbot_scale", false);
    _countsPerGram = _prefs.getFloat("cpg", 0.0f);
    if (_countsPerGram <= 0.0f) {
      _countsPerGram = 0.0f;
    }
  }

  bool isCalibrated() const { return _countsPerGram > 0.0f; }

  float countsPerGram() const { return _countsPerGram; }

  bool setCountsPerGram(float cpg) {
    if (!(cpg > 0.0f)) {
      return false;
    }
    _countsPerGram = cpg;
    _prefs.putFloat("cpg", _countsPerGram);
    return true;
  }

  bool tare(int reads = cfg::DEFAULT_TARE_READS) {
    long avg = 0;
    if (!readAverageRaw(reads, avg)) {
      return false;
    }
    _tareOffset = avg;
    return true;
  }

  bool calibrateWithKnownWeight(float knownGrams, int reads = cfg::DEFAULT_SCALE_READS) {
    if (!(knownGrams > 0.0f)) {
      return false;
    }

    long avg = 0;
    if (!readAverageRaw(reads, avg)) {
      return false;
    }

    const float delta = static_cast<float>(avg - _tareOffset);
    if (fabsf(delta) < 1.0f) {
      return false;
    }
    return setCountsPerGram(delta / knownGrams);
  }

  bool weightGrams(float &grams, int reads = cfg::DEFAULT_SCALE_READS) {
    long avg = 0;
    if (!readAverageRaw(reads, avg)) {
      return false;
    }
    if (!isCalibrated()) {
      grams = NAN;
      return true;
    }
    grams = static_cast<float>(avg - _tareOffset) / _countsPerGram;
    return true;
  }

  bool raw(long &raw) { return _hx711.read(raw, cfg::HX711_TIMEOUT_MS); }

  long tareOffset() const { return _tareOffset; }

private:
  bool readAverageRaw(int reads, long &avg) {
    if (reads <= 0) {
      return false;
    }

    int ok = 0;
    long sum = 0;
    for (int i = 0; i < reads; ++i) {
      long raw = 0;
      if (_hx711.read(raw, cfg::HX711_TIMEOUT_MS)) {
        sum += raw;
        ok++;
      }
      delay(4);
    }
    if (ok <= 0) {
      return false;
    }
    avg = sum / ok;
    return true;
  }

  HX711 &_hx711;
  Preferences _prefs;
  long _tareOffset = 0;
  float _countsPerGram = 0.0f;
};

HX711 hx711(cfg::HX711_DATA_PIN, cfg::HX711_CLOCK_PIN);
Scale scale(hx711);

String inputBuf;
bool emergencyStopLatched = false;
bool pumpActive[cfg::PUMP_COUNT] = {false, false, false, false};
uint32_t pumpOffAtMs[cfg::PUMP_COUNT] = {0, 0, 0, 0};

bool parseGorM(const String &line, char &kind, int &major, bool &hasMinor, int &minor) {
  int i = 0;
  while (i < line.length() && line[i] == ' ') {
    i++;
  }
  if (i >= line.length()) {
    return false;
  }

  kind = line[i];
  if (kind != 'G' && kind != 'M') {
    return false;
  }
  i++;

  int startMajor = i;
  while (i < line.length() && isDigit(line[i])) {
    i++;
  }
  if (i == startMajor) {
    return false;
  }
  major = line.substring(startMajor, i).toInt();

  hasMinor = false;
  minor = 0;
  if (i < line.length() && line[i] == '.') {
    i++;
    const int startMinor = i;
    while (i < line.length() && isDigit(line[i])) {
      i++;
    }
    if (i == startMinor) {
      return false;
    }
    hasMinor = true;
    minor = line.substring(startMinor, i).toInt();
  }
  return true;
}

bool parseParamFloat(const String &line, char key, float &value) {
  const int idx = line.indexOf(key);
  if (idx < 0 || idx + 1 >= line.length()) {
    return false;
  }
  int start = idx + 1;
  while (start < line.length() && line[start] == ' ') {
    start++;
  }
  int end = start;
  if (end < line.length() && (line[end] == '+' || line[end] == '-')) {
    end++;
  }
  while (end < line.length() && (isDigit(line[end]) || line[end] == '.')) {
    end++;
  }
  if (end <= start) {
    return false;
  }
  value = line.substring(start, end).toFloat();
  return true;
}

bool parseParamUInt(const String &line, char key, uint32_t &value) {
  float tmp = 0.0f;
  if (!parseParamFloat(line, key, tmp) || tmp < 0.0f) {
    return false;
  }
  value = static_cast<uint32_t>(tmp + 0.5f);
  return true;
}

bool parseParamInt(const String &line, char key, int &value) {
  float tmp = 0.0f;
  if (!parseParamFloat(line, key, tmp)) {
    return false;
  }
  value = static_cast<int>(tmp + (tmp >= 0.0f ? 0.5f : -0.5f));
  return true;
}

uint8_t pumpActiveLevel() { return cfg::PUMP_ACTIVE_LOW ? LOW : HIGH; }
uint8_t pumpInactiveLevel() { return cfg::PUMP_ACTIVE_LOW ? HIGH : LOW; }

void setPump(size_t idx, bool on) {
  if (idx >= cfg::PUMP_COUNT) {
    return;
  }
  digitalWrite(cfg::PUMP_PINS[idx], on ? pumpActiveLevel() : pumpInactiveLevel());
  pumpActive[idx] = on;
}

void stopAllPumps() {
  for (size_t i = 0; i < cfg::PUMP_COUNT; ++i) {
    setPump(i, false);
    pumpOffAtMs[i] = 0;
  }
}

void updateScheduledPumps() {
  const uint32_t now = millis();
  for (size_t i = 0; i < cfg::PUMP_COUNT; ++i) {
    if (pumpActive[i] && pumpOffAtMs[i] > 0 && static_cast<int32_t>(now - pumpOffAtMs[i]) >= 0) {
      setPump(i, false);
      pumpOffAtMs[i] = 0;
    }
  }
}

void fillEnd(const char *reason, float dispensedG, uint32_t durationMs) {
  Serial.printf("[FILL_END] reason=%s dispensed=%.1fg duration=%lums\r\n", reason, dispensedG,
                durationMs);
}

bool runPumpTimed(int pumpIdx, uint32_t durationMs, bool wait) {
  if (pumpIdx < 0 || pumpIdx >= static_cast<int>(cfg::PUMP_COUNT)) {
    Serial.println("ERROR invalid pump index");
    return false;
  }
  if (emergencyStopLatched) {
    Serial.println("ERROR stopped: send M1 first");
    return false;
  }
  if (durationMs == 0) {
    setPump(static_cast<size_t>(pumpIdx), false);
    pumpOffAtMs[pumpIdx] = 0;
    return true;
  }

  setPump(static_cast<size_t>(pumpIdx), true);
  if (!wait) {
    pumpOffAtMs[pumpIdx] = millis() + durationMs;
    Serial.printf("OK pump %d running for %lums\r\n", pumpIdx, durationMs);
    return true;
  }

  const uint32_t start = millis();
  while (millis() - start < durationMs) {
    if (emergencyStopLatched) {
      setPump(static_cast<size_t>(pumpIdx), false);
      pumpOffAtMs[pumpIdx] = 0;
      Serial.println("ERROR stopped");
      return false;
    }
    updateScheduledPumps();
    delay(2);
  }
  setPump(static_cast<size_t>(pumpIdx), false);
  pumpOffAtMs[pumpIdx] = 0;
  Serial.printf("OK pump %d done\r\n", pumpIdx);
  return true;
}

bool runPumpToWeight(int pumpIdx, float targetGrams) {
  if (pumpIdx < 0 || pumpIdx >= static_cast<int>(cfg::PUMP_COUNT)) {
    Serial.println("ERROR invalid pump index");
    return false;
  }
  if (emergencyStopLatched) {
    Serial.println("ERROR stopped: send M1 first");
    return false;
  }
  if (!scale.isCalibrated()) {
    Serial.println("ERROR fill failed: scale not calibrated, run G3.2 or G3.3 first");
    return false;
  }
  if (!(targetGrams > 0.0f)) {
    fillEnd("zero_target", 0.0f, 0);
    return true;
  }

  float baseline = 0.0f;
  if (!scale.weightGrams(baseline) || isnan(baseline)) {
    Serial.println("ERROR fill failed: HX711 not responding");
    return false;
  }

  const float stopAt = baseline + targetGrams;
  Serial.printf("[FILL_START] pump=%d target=%.1fg stop_at=%.1fg baseline=%.2fg\r\n", pumpIdx,
                targetGrams, stopAt, baseline);

  setPump(static_cast<size_t>(pumpIdx), true);
  const uint32_t startMs = millis();
  const uint32_t timeoutMs =
      cfg::FILL_BASE_TIMEOUT_MS + static_cast<uint32_t>(targetGrams * cfg::FILL_TIMEOUT_PER_G_MS);

  float lastProgressWeight = baseline;
  uint32_t lastProgressMs = startMs;
  float current = baseline;
  const char *reason = "target_reached";

  while (true) {
    if (emergencyStopLatched) {
      reason = "stopped";
      break;
    }

    delay(cfg::FILL_POLL_MS);
    updateScheduledPumps();

    if (!scale.weightGrams(current) || isnan(current)) {
      reason = "hx711_fail";
      break;
    }

    if (current >= stopAt) {
      reason = "target_reached";
      break;
    }

    if (fabsf(current - lastProgressWeight) >= cfg::FILL_STALL_DELTA_G) {
      lastProgressWeight = current;
      lastProgressMs = millis();
    } else if (millis() - lastProgressMs > cfg::FILL_STALL_MS) {
      reason = "pump_failure";
      break;
    }

    if (millis() - startMs > timeoutMs) {
      reason = "timeout";
      break;
    }
  }

  setPump(static_cast<size_t>(pumpIdx), false);
  pumpOffAtMs[pumpIdx] = 0;

  Serial.printf("[FILL] drain wait %lums\r\n", cfg::FILL_DRAIN_WAIT_MS);
  delay(cfg::FILL_DRAIN_WAIT_MS);

  float endWeight = current;
  float measured = endWeight - baseline;
  if (!scale.weightGrams(endWeight) || isnan(endWeight)) {
    measured = current - baseline;
  } else {
    measured = endWeight - baseline;
  }

  fillEnd(reason, measured, millis() - startMs);
  if (strcmp(reason, "target_reached") == 0) {
    return true;
  }
  Serial.printf("ERROR fill failed: %s\r\n", reason);
  return false;
}

void printHelp() {
  Serial.println("BarBot pump+scale ESP32");
  Serial.println("Commands:");
  Serial.println("  G2 I{pump} D{ms}       - run pump non-blocking");
  Serial.println("  G2.1 I{pump} D{ms}     - run pump and wait");
  Serial.println("  G3                     - read current weight (g)");
  Serial.println("  G3.1                   - tare scale");
  Serial.println("  G3.2 W{grams}          - calibrate with known weight");
  Serial.println("  G3.3 F{countsPerGram}  - set calibration factor directly");
  Serial.println("  G3.4 N{samples}        - debug raw + grams samples");
  Serial.println("  G4 I{pump} W{grams}    - run pump until target grams dispensed");
  Serial.println("  M0 / M0.1              - stop all pumps immediately");
  Serial.println("  M1                     - clear stop latch");
}

void handleCommand(const String &rawLine) {
  String line = rawLine;
  line.trim();
  if (line.isEmpty()) {
    return;
  }
  line.toUpperCase();

  if (line == "HELP" || line == "?") {
    printHelp();
    return;
  }

  char kind = '\0';
  int major = 0;
  int minor = 0;
  bool hasMinor = false;
  if (!parseGorM(line, kind, major, hasMinor, minor)) {
    Serial.printf("ERROR invalid command: %s\r\n", line.c_str());
    return;
  }

  if (kind == 'M') {
    if (major == 0) {
      emergencyStopLatched = true;
      stopAllPumps();
      Serial.println("OK stopped");
      return;
    }
    if (major == 1 && !hasMinor) {
      emergencyStopLatched = false;
      Serial.println("OK continue");
      return;
    }
    Serial.printf("ERROR invalid M command: %s\r\n", line.c_str());
    return;
  }

  // G commands.
  if (major == 2 && (!hasMinor || minor == 1)) {
    int pumpIdx = -1;
    uint32_t durationMs = 0;
    if (!parseParamInt(line, 'I', pumpIdx) || !parseParamUInt(line, 'D', durationMs)) {
      Serial.println("ERROR G2 requires I and D params");
      return;
    }
    runPumpTimed(pumpIdx, durationMs, hasMinor && minor == 1);
    return;
  }

  if (major == 3 && !hasMinor) {
    float grams = 0.0f;
    if (!scale.weightGrams(grams)) {
      Serial.println("ERROR scale read failed");
      return;
    }
    if (isnan(grams)) {
      Serial.println("WEIGHT uncalibrated");
      return;
    }
    Serial.printf("WEIGHT %.2fg\r\n", grams);
    return;
  }

  if (major == 3 && hasMinor && minor == 1) {
    if (!scale.tare()) {
      Serial.println("ERROR tare failed");
      return;
    }
    Serial.printf("Scale tared (offset=%ld)\r\n", scale.tareOffset());
    return;
  }

  if (major == 3 && hasMinor && minor == 2) {
    float knownG = 0.0f;
    if (!parseParamFloat(line, 'W', knownG)) {
      Serial.println("ERROR G3.2 requires W param");
      return;
    }
    if (!scale.calibrateWithKnownWeight(knownG)) {
      Serial.println("ERROR calibration failed");
      return;
    }
    Serial.printf("Calibrated: %.4f counts/gram\r\n", scale.countsPerGram());
    return;
  }

  if (major == 3 && hasMinor && minor == 3) {
    float cpg = 0.0f;
    if (!parseParamFloat(line, 'F', cpg)) {
      Serial.println("ERROR G3.3 requires F param");
      return;
    }
    if (!scale.setCountsPerGram(cpg)) {
      Serial.println("ERROR invalid calibration factor");
      return;
    }
    Serial.printf("Calibration factor set: %.4f counts/gram\r\n", scale.countsPerGram());
    return;
  }

  if (major == 3 && hasMinor && minor == 4) {
    int samples = 0;
    if (!parseParamInt(line, 'N', samples) || samples <= 0) {
      Serial.println("ERROR G3.4 requires N>0");
      return;
    }
    for (int i = 0; i < samples; ++i) {
      long raw = 0;
      float grams = 0.0f;
      const bool okRaw = scale.raw(raw);
      const bool okWeight = scale.weightGrams(grams, 1);
      if (!okRaw || !okWeight) {
        Serial.printf("[DBG] %d read failed\r\n", i);
      } else if (isnan(grams)) {
        Serial.printf("[DBG] %d raw=%ld weight=uncalibrated\r\n", i, raw);
      } else {
        Serial.printf("[DBG] %d raw=%ld weight=%.2fg\r\n", i, raw, grams);
      }
      delay(30);
    }
    Serial.println("[DBG] done");
    return;
  }

  if (major == 4 && !hasMinor) {
    int pumpIdx = -1;
    float targetG = 0.0f;
    if (!parseParamInt(line, 'I', pumpIdx) || !parseParamFloat(line, 'W', targetG)) {
      Serial.println("ERROR G4 requires I and W params");
      return;
    }
    runPumpToWeight(pumpIdx, targetG);
    return;
  }

  Serial.printf("ERROR invalid G command: %s\r\n", line.c_str());
}

void setup() {
  Serial.begin(cfg::SERIAL_BAUD);
  delay(100);

  for (size_t i = 0; i < cfg::PUMP_COUNT; ++i) {
    pinMode(cfg::PUMP_PINS[i], OUTPUT);
    digitalWrite(cfg::PUMP_PINS[i], pumpInactiveLevel());
  }

  scale.begin();
  inputBuf.reserve(160);

  Serial.println();
  printHelp();
  Serial.printf("[SCALE] DATA pin: GPIO%d, CLOCK pin: GPIO%d\r\n", cfg::HX711_DATA_PIN,
                cfg::HX711_CLOCK_PIN);
  if (scale.isCalibrated()) {
    Serial.printf("[SCALE] Loaded calibration: %.4f counts/gram\r\n", scale.countsPerGram());
  } else {
    Serial.println("[SCALE] No calibration saved (run G3.2 or G3.3)");
  }
}

void loop() {
  while (Serial.available() > 0) {
    const int c = Serial.read();
    if (c < 0) {
      break;
    }
    if (c == '\n' || c == '\r') {
      if (!inputBuf.isEmpty()) {
        handleCommand(inputBuf);
        inputBuf = "";
      }
    } else if (inputBuf.length() < 159) {
      inputBuf += static_cast<char>(c);
    }
  }

  updateScheduledPumps();
  delay(2);
}

