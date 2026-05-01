#include <Arduino.h>
#include <Preferences.h>

// ---------------------------------------------------------------------------
// Pin assignments
// ---------------------------------------------------------------------------
#define HX711_CLK  13
#define HX711_DATA 14

// L298N pump channels (IN1/IN2 per pump)
// Pump index 0: GPIO27, GPIO26
// Pump index 1: GPIO33, GPIO25 (reversed polarity)
// Pump index 2: GPIO23, GPIO22 (reversed polarity)
// Pump index 3: GPIO18, GPIO19 (reversed polarity)
static const int PUMP_PIN_A[4] = {27, 33, 23, 18};
static const int PUMP_PIN_B[4] = {26, 25, 22, 19};
static bool pumpActive[4] = {false, false, false, false};
static uint32_t pumpOffAtMs[4] = {0, 0, 0, 0};

// ---------------------------------------------------------------------------
// Fill constants
// ---------------------------------------------------------------------------
#define STOP_FRACTION  0.98f
#define DRAIN_WAIT_MS  2000     // ms to wait after fill before signalling done

// ---------------------------------------------------------------------------
// Forward declarations
// ---------------------------------------------------------------------------
static void pumpStop(int pumpIdx);
static void pumpReverse(int pumpIdx);

// ---------------------------------------------------------------------------
// HX711 — bit-bang driver
// ---------------------------------------------------------------------------
class HX711 {
public:
    HX711(int clk, int data) : _clk(clk), _data(data) {}

    void begin() {
        pinMode(_clk,  OUTPUT);
        pinMode(_data, INPUT);
        digitalWrite(_clk, LOW);
        delay(500);
    }

    bool isReady() { return digitalRead(_data) == LOW; }

    int32_t read() {
        if (!_waitReady(1000)) return INT32_MIN;
        int32_t v = 0;
        for (int i = 0; i < 24; i++) {
            digitalWrite(_clk, HIGH); delayMicroseconds(1);
            v = (v << 1) | (digitalRead(_data) ? 1 : 0);
            digitalWrite(_clk, LOW);  delayMicroseconds(1);
        }
        // extra clock pulse selects gain 128 for next read
        digitalWrite(_clk, HIGH); delayMicroseconds(1);
        digitalWrite(_clk, LOW);
        if (v & 0x800000) v |= 0xFF000000;  // sign-extend 24-bit
        return v;
    }

private:
    int _clk, _data;

    bool _waitReady(uint32_t timeout_ms) {
        uint32_t t0 = millis();
        while (digitalRead(_data) == HIGH) {
            if (millis() - t0 > timeout_ms) return false;
            delayMicroseconds(10);
        }
        return true;
    }
};

// ---------------------------------------------------------------------------
// Kalman filter (1-D, constant process/measurement noise)
// ---------------------------------------------------------------------------
class Kalman {
public:
    Kalman() : _estimate(0), _errorCov(1000.0f), _initialized(false) {}

    void reset(float initial) {
        _estimate    = initial;
        _errorCov    = 1000.0f;
        _initialized = true;
    }

    float update(float z) {
        if (!_initialized) reset(z);
        const float Q = 500.0f, R = 2500.0f;
        float p  = _errorCov + Q;
        float k  = p / (p + R);
        _estimate  = _estimate + k * (z - _estimate);
        _errorCov  = (1.0f - k) * p;
        return _estimate;
    }

    float estimate()    const { return _estimate; }
    bool  initialized() const { return _initialized; }

private:
    float _estimate, _errorCov;
    bool  _initialized;
};

// ---------------------------------------------------------------------------
// Scale
// ---------------------------------------------------------------------------
class Scale {
public:
    static constexpr float DEFAULT_CPG = 27.06f;

    Scale(HX711& hx711) : _hx711(hx711), _tareOffset(0), _cpg(DEFAULT_CPG) {}

    void begin() {
        _hx711.begin();
        _loadCalibration();
        Serial.println("[SCALE] ready");
        Serial.printf("[SCALE] DATA pin: %s\n", _hx711.isReady() ? "LOW (ready)" : "HIGH (not ready)");
    }

    // G3.1 — tare
    bool tare(int samples = 8) {
        int64_t sum = 0; int count = 0;
        for (int i = 0; i < samples; i++) {
            int32_t raw = _hx711.read();
            if (raw != INT32_MIN) { sum += raw; count++; Serial.printf("[TARE] %d: %ld\n", i + 1, (long)raw); }
            else                  { Serial.printf("[TARE] %d: FAILED\n", i + 1); }
        }
        if (count < samples / 2) { Serial.printf("ERROR tare failed (%d/%d reads)\n", count, samples); return false; }
        _tareOffset = (int32_t)(sum / count);
        _kalman.reset(0.0f);
        Serial.printf("Scale tared (offset=%ld, %d/%d reads)\n", (long)_tareOffset, count, samples);
        return true;
    }

    // G3.2 W{g} — calibrate with known weight already on scale
    bool calibrate(float knownGrams) {
        int32_t raw = _hx711.read();
        if (raw == INT32_MIN) { Serial.println("ERROR calibration failed: no HX711 response"); return false; }
        float tared = (float)(raw - _tareOffset);
        if (fabsf(tared) < 1.0f) { Serial.println("ERROR calibration failed: reading near zero, tare first"); return false; }
        _cpg = tared / knownGrams;
        _saveCalibration();
        Serial.printf("Calibrated: %.4f counts/gram (tared=%.0f known=%.1fg)\n", _cpg, tared, knownGrams);
        return true;
    }

    // G3.3 F{cpg} — restore calibration factor from host
    void setCountsPerGram(float cpg) {
        _cpg = cpg;
        _saveCalibration();
        Serial.printf("Scale calibration restored: %.4f counts/gram\n", _cpg);
    }

    // G3 — Kalman-filtered weight reading
    float weightGrams(int samples = 3) {
        int64_t sum = 0; int count = 0;
        for (int i = 0; i < samples; i++) {
            int32_t raw = _hx711.read();
            if (raw != INT32_MIN) { sum += (raw - _tareOffset); count++; }
        }
        if (count == 0) return NAN;
        float measured = (float)(sum / count) / _cpg;
        // Re-seed the filter if it is far from the measurement so the first
        // read after a tare or a large weight change converges immediately.
        if (!_kalman.initialized() || fabsf(measured - _kalman.estimate()) > 50.0f)
            _kalman.reset(measured);
        return _kalman.update(measured);
    }

    // G3.4 N{n} — raw debug reads
    void debug(int n) {
        Serial.printf("[DBG] DATA pin: %s\n", _hx711.isReady() ? "LOW (ready)" : "HIGH (not ready)");
        for (int i = 0; i < n; i++) {
            uint32_t t0  = millis();
            int32_t  raw = _hx711.read();
            uint32_t dt  = millis() - t0;
            if (raw != INT32_MIN) Serial.printf("[DBG] %d  wait=%lums raw=%ld\n", i, (unsigned long)dt, (long)raw);
            else                  Serial.printf("[DBG] %d  TIMEOUT after %lums\n", i, (unsigned long)dt);
        }
        Serial.println("[DBG] done");
    }

    int32_t readRaw()       { return _hx711.read(); }
    int32_t tareOffset()    { return _tareOffset; }
    float   countsPerGram() { return _cpg; }

private:
    HX711&      _hx711;
    Kalman      _kalman;
    int32_t     _tareOffset;
    float       _cpg;
    Preferences _prefs;

    void _saveCalibration() {
        _prefs.begin("scale", false);
        _prefs.putFloat("cpg", _cpg);
        _prefs.end();
    }

    void _loadCalibration() {
        _prefs.begin("scale", true);
        _cpg = _prefs.getFloat("cpg", DEFAULT_CPG);
        _prefs.end();
        Serial.printf("[SCALE] loaded calibration: %.4f counts/gram\n", _cpg);
    }
};

// ---------------------------------------------------------------------------
// Fill loop — G4 I{pump} W{grams}
//


// ---------------------------------------------------------------------------

static void fillEnd(const char* reason, float dispensed, uint32_t elapsed, int pumpIdx) {
    pumpStop(pumpIdx);
    Serial.printf("[RETRACT_START]\n");
    Serial.flush();
    delay(600);
    pumpReverse(pumpIdx);
    delay(1000);
    pumpStop(pumpIdx);
    Serial.printf("[FILL_END] reason=%s dispensed=%.1fg duration=%lums\n",
                  reason, dispensed, (unsigned long)elapsed);
    Serial.flush();
}

static bool pumpValid(int pumpIdx) { return pumpIdx >= 0 && pumpIdx < 4; }

static void pumpStop(int pumpIdx) {
    if (!pumpValid(pumpIdx)) return;
    digitalWrite(PUMP_PIN_A[pumpIdx], LOW);
    digitalWrite(PUMP_PIN_B[pumpIdx], LOW);
    pumpActive[pumpIdx] = false;
    pumpOffAtMs[pumpIdx] = 0;
}

static void pumpStart(int pumpIdx) {
    if (!pumpValid(pumpIdx)) return;
    // Forward direction on L298N.
    digitalWrite(PUMP_PIN_A[pumpIdx], HIGH);
    digitalWrite(PUMP_PIN_B[pumpIdx], LOW);
    pumpActive[pumpIdx] = true;
}

static void pumpReverse(int pumpIdx) {
    if (!pumpValid(pumpIdx)) return;
    // Reverse direction on L298N.
    digitalWrite(PUMP_PIN_A[pumpIdx], LOW);
    digitalWrite(PUMP_PIN_B[pumpIdx], HIGH);
    pumpActive[pumpIdx] = true;
}

static void stopAllPumps() {
    for (int i = 0; i < 4; i++) pumpStop(i);
}

static void updatePumpSchedules() {
    uint32_t now = millis();
    for (int i = 0; i < 4; i++) {
        if (pumpActive[i] && pumpOffAtMs[i] != 0 && (int32_t)(now - pumpOffAtMs[i]) >= 0) {
            pumpStop(i);
        }
    }
}

static void runPumpTimed(int pumpIdx, uint32_t durationMs, bool wait) {
    if (!pumpValid(pumpIdx)) {
        Serial.println("ERROR invalid pump index");
        return;
    }
    if (durationMs == 0) {
        pumpStop(pumpIdx);
        Serial.printf("OK pump=%d stopped\n", pumpIdx);
        return;
    }
    pumpStart(pumpIdx);
    if (!wait) {
        pumpOffAtMs[pumpIdx] = millis() + durationMs;
        Serial.printf("OK pump=%d running_for=%lums\n", pumpIdx, (unsigned long)durationMs);
        return;
    }
    uint32_t t0 = millis();
    while ((millis() - t0) < durationMs) delay(2);
    pumpStop(pumpIdx);
    Serial.printf("OK pump=%d done\n", pumpIdx);
}

static void fillLoop(Scale& scale, int pumpIdx, float targetGrams) {
    const float cpg = scale.countsPerGram();
    if (cpg < 1.0f) {
        Serial.println("ERROR fill failed: scale not calibrated, run G3.2 first");
        return;
    }
    if (!pumpValid(pumpIdx)) {
        Serial.println("ERROR invalid pump index");
        return;
    }
    if (targetGrams <= 0.0f) {
        fillEnd("zero_target", 0.0f, 0, pumpIdx);
        return;
    }

    const float OUTLIER_THRESHOLD_G = 20.0f;
    const int   OUTLIER_TREND_COUNT = 8;
    const int   MAX_HX711_ERRORS    = 5;
    const float MIN_PROGRESS_G      = 1.0f;
    const int   NO_PROGRESS_LIMIT   = 50;
    const float RATE_WINDOW_G       = 10.0f;
    const int   RATE_WINDOW_SAMPLES = 120;
    const float FILL_TIMEOUT_MS     = 60000.0f;

    // Establish baseline before pump starts
    int64_t bSum = 0; int bCount = 0;
    for (int i = 0; i < 2; i++) {
        int32_t raw = scale.readRaw();
        if (raw != INT32_MIN) { bSum += (raw - scale.tareOffset()); bCount++; }
    }
    if (bCount == 0) { Serial.println("ERROR fill failed: HX711 not responding"); return; }

    float baseline = ((float)bSum / bCount) / cpg;
    float stopAt   = targetGrams * STOP_FRACTION;

    Kalman kalman;
    kalman.reset(baseline);

    Serial.printf("[FILL_START] pump=%d target=%.1fg stop_at=%.1fg baseline=%.2fg\n",
        pumpIdx, targetGrams, stopAt, baseline);

    uint32_t startMs = millis();
    pumpStart(pumpIdx);

    int   hx711Errors   = 0;
    float lastWeight    = baseline;
    int   noProgressCnt = 0;
    float outlierBuf[OUTLIER_TREND_COUNT] = {};
    int   outlierCount  = 0;
    float weightHistory[RATE_WINDOW_SAMPLES] = {};
    int   histIdx = 0, histCount = 0;
    float dispensed = 0.0f;
    uint32_t lastWeightLogMs = 0;

    while (true) {
        uint32_t elapsed = millis() - startMs;

        if ((float)elapsed >= FILL_TIMEOUT_MS) {
            fillEnd("timeout", dispensed, elapsed, pumpIdx);
            return;
        }

        int32_t raw = scale.readRaw();
        if (raw == INT32_MIN) {
            Serial.printf("[HX711] read error %d/%d\n", ++hx711Errors, MAX_HX711_ERRORS);
            if (hx711Errors >= MAX_HX711_ERRORS) {
                fillEnd("hx711_error", dispensed, elapsed, pumpIdx);
                return;
            }
            continue;
        }
        hx711Errors = 0;

        float weightG = (float)(raw - scale.tareOffset()) / cpg;

        // Outlier rejection with sustained-trend detection
        if (fabsf(weightG - kalman.estimate()) > OUTLIER_THRESHOLD_G) {
            outlierBuf[outlierCount % OUTLIER_TREND_COUNT] = weightG;
            outlierCount++;
            if (outlierCount >= OUTLIER_TREND_COUNT) {
                bool allAbove = true, allBelow = true;
                for (int i = 0; i < OUTLIER_TREND_COUNT; i++) {
                    if (outlierBuf[i] <= kalman.estimate()) allAbove = false;
                    if (outlierBuf[i] >= kalman.estimate()) allBelow = false;
                }
                if (allAbove || allBelow) {
                    float sorted[OUTLIER_TREND_COUNT];
                    memcpy(sorted, outlierBuf, sizeof(sorted));
                    for (int i = 0; i < OUTLIER_TREND_COUNT - 1; i++)
                        for (int j = i + 1; j < OUTLIER_TREND_COUNT; j++)
                            if (sorted[j] < sorted[i]) { float t = sorted[i]; sorted[i] = sorted[j]; sorted[j] = t; }
                    float median = sorted[OUTLIER_TREND_COUNT / 2];
                    Serial.printf("[TREND] t=%lums resetting Kalman to %.2fg\n", (unsigned long)elapsed, median);
                    kalman.reset(median);
                    outlierCount = 0;
                    continue;
                }
            }
            Serial.printf("[OUTLIER] t=%lums measured=%.2fg estimate=%.2fg — skipped\n",
                (unsigned long)elapsed, weightG, kalman.estimate());
            continue;
        }
        outlierCount = 0;

        float filtered = kalman.update(weightG);
        dispensed = baseline - filtered;
        if (dispensed < 0.0f) dispensed = 0.0f;

        // Periodic weight log (~1 Hz) so Python watchdog can reset its timer
        if (elapsed - lastWeightLogMs >= 1000) {
            Serial.printf("[FILL_WEIGHT] t=%lums w=%.1fg\n",
                          (unsigned long)elapsed, dispensed);
            lastWeightLogMs = elapsed;
        }

        // No-progress check
        if (fabsf(filtered - lastWeight) < MIN_PROGRESS_G) {
            if (++noProgressCnt >= NO_PROGRESS_LIMIT) {
                fillEnd("empty_or_blocked", dispensed, elapsed, pumpIdx);
                return;
            }
        } else {
            noProgressCnt = 0;
            lastWeight    = filtered;
        }

        // Sliding window rate check
        int slot = histIdx % RATE_WINDOW_SAMPLES;
        weightHistory[slot] = filtered;
        histIdx++;
        if (histCount < RATE_WINDOW_SAMPLES) histCount++;
        if (histCount >= RATE_WINDOW_SAMPLES) {
            float oldest = weightHistory[histIdx % RATE_WINDOW_SAMPLES];
            float change = fabsf(oldest - filtered);
            if (change < RATE_WINDOW_G) {
                fillEnd("pump_failure", dispensed, elapsed, pumpIdx);
                return;
            }
        }

        if (dispensed >= stopAt) {
            fillEnd("target_reached", dispensed, elapsed, pumpIdx);
            return;
        }
    }
}

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------
HX711 hx711(HX711_CLK, HX711_DATA);
Scale scale(hx711);

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    for (int i = 0; i < 4; i++) {
        pinMode(PUMP_PIN_A[i], OUTPUT);
        pinMode(PUMP_PIN_B[i], OUTPUT);
    }
    stopAllPumps();

    Serial.println("BarBot scale ESP32");
    scale.begin();
    Serial.println("Commands: G2 I{n} D{ms}  G2.1 I{n} D{ms}  G3  G3.1  G3.2 W{g}  G3.3 F{cpg}  G3.4 N{n}  G4 I{n} W{g}  M0");
}

// ---------------------------------------------------------------------------
// Command handler
// ---------------------------------------------------------------------------
void handleCommand(const String& line) {
    if (line == "G3") {
        float w = scale.weightGrams();
        if (isnan(w)) Serial.println("ERROR scale read failed");
        else          Serial.printf("Weight: %.2fg\n", w);

    } else if (line == "G3.1") {
        scale.tare();

    } else if (line.startsWith("G3.2")) {
        int i = line.indexOf('W');
        if (i < 0) { Serial.println("ERROR G3.2 requires W param"); return; }
        scale.calibrate(line.substring(i + 1).toFloat());

    } else if (line.startsWith("G3.3")) {
        int i = line.indexOf('F');
        if (i < 0) { Serial.println("ERROR G3.3 requires F param"); return; }
        scale.setCountsPerGram(line.substring(i + 1).toFloat());

    } else if (line.startsWith("G3.4")) {
        int i = line.indexOf('N');
        scale.debug(i >= 0 ? line.substring(i + 1).toInt() : 10);

    } else if (line.startsWith("G4")) {
        int iIdx = line.indexOf('I'), wIdx = line.indexOf('W');
        if (iIdx < 0 || wIdx < 0) { Serial.println("ERROR G4 requires I and W params"); return; }
        int   pumpIdx = line.substring(iIdx + 1).toInt();
        float grams   = line.substring(wIdx + 1).toFloat();
        fillLoop(scale, pumpIdx, grams);

    } else if (line.startsWith("G2.1")) {
        int iIdx = line.indexOf('I'), dIdx = line.indexOf('D');
        if (iIdx < 0 || dIdx < 0) { Serial.println("ERROR G2.1 requires I and D params"); return; }
        int pumpIdx = line.substring(iIdx + 1).toInt();
        uint32_t ms = (uint32_t)line.substring(dIdx + 1).toInt();
        runPumpTimed(pumpIdx, ms, true);

    } else if (line.startsWith("G2")) {
        int iIdx = line.indexOf('I'), dIdx = line.indexOf('D');
        if (iIdx < 0 || dIdx < 0) { Serial.println("ERROR G2 requires I and D params"); return; }
        int pumpIdx = line.substring(iIdx + 1).toInt();
        uint32_t ms = (uint32_t)line.substring(dIdx + 1).toInt();
        runPumpTimed(pumpIdx, ms, false);

    } else if (line == "M0" || line == "M0.1") {
        stopAllPumps();
        Serial.println("OK stopped");

    } else if (line == "M115") {
        Serial.println("FIRMWARE_NAME:barbot-scale FIRMWARE_VERSION:1.0");

    } else {
        Serial.printf("ERROR unknown command: %s\n", line.c_str());
    }
}

// ---------------------------------------------------------------------------
// Loop
// ---------------------------------------------------------------------------
String inputBuf;

void loop() {
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            inputBuf.trim();
            if (inputBuf.length() > 0) handleCommand(inputBuf);
            inputBuf = "";
        } else {
            inputBuf += c;
        }
    }
    updatePumpSchedules();
}
