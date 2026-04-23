#include <Arduino.h>
#include <Preferences.h>

// ---------------------------------------------------------------------------
// Pin assignments
// ---------------------------------------------------------------------------
#define HX711_CLK  13
#define HX711_DATA 14

// ---------------------------------------------------------------------------
// Fill constants
// ---------------------------------------------------------------------------
#define STOP_FRACTION  0.98f
#define DRAIN_WAIT_MS  2000     // ms to wait after fill before signalling done

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
        return _kalman.update((float)(sum / count) / _cpg);
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
// The pump is driven by relays on the C3. This ESP32 only monitors the scale
// and sends [FILL_END] when the target weight is reached. The host (Pi) is
// responsible for running the pump relay (G2 on C3) concurrently and stopping
// it when it receives [FILL_END].
//
// [FILL_END] is sent before the drain wait so the host can stop the pump and
// move the cart immediately while residue drains from the tubing.
// ---------------------------------------------------------------------------

static void fillEnd(const char* reason, float dispensed, uint32_t elapsed) {
    Serial.printf("[FILL_END] reason=%s dispensed=%.1fg duration=%lums\n",
                  reason, dispensed, (unsigned long)elapsed);
    Serial.flush();
    Serial.printf("[FILL] drain wait %dms\n", DRAIN_WAIT_MS);
    delay(DRAIN_WAIT_MS);
}

static void fillLoop(Scale& scale, int pumpIdx, float targetGrams) {
    const float cpg = scale.countsPerGram();
    if (cpg < 1.0f) {
        Serial.println("ERROR fill failed: scale not calibrated, run G3.2 first");
        return;
    }
    if (targetGrams <= 0.0f) {
        Serial.println("[FILL_END] reason=zero_target dispensed=0.0g duration=0ms");
        return;
    }

    const float OUTLIER_THRESHOLD_G = 20.0f;
    const int   OUTLIER_TREND_COUNT = 8;
    const int   MAX_HX711_ERRORS    = 5;
    const float MIN_PROGRESS_G      = 1.0f;
    const int   NO_PROGRESS_LIMIT   = 50;
    const float RATE_WINDOW_G       = 25.0f;
    const int   RATE_WINDOW_SAMPLES = 90;
    const float FILL_TIMEOUT_MS     = 30000.0f;

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

    int   hx711Errors   = 0;
    float lastWeight    = baseline;
    int   noProgressCnt = 0;
    float outlierBuf[OUTLIER_TREND_COUNT] = {};
    int   outlierCount  = 0;
    float weightHistory[RATE_WINDOW_SAMPLES] = {};
    int   histIdx = 0, histCount = 0;
    float dispensed = 0.0f;

    while (true) {
        uint32_t elapsed = millis() - startMs;

        if ((float)elapsed >= FILL_TIMEOUT_MS) {
            fillEnd("timeout", dispensed, elapsed);
            return;
        }

        int32_t raw = scale.readRaw();
        if (raw == INT32_MIN) {
            Serial.printf("[HX711] read error %d/%d\n", ++hx711Errors, MAX_HX711_ERRORS);
            if (hx711Errors >= MAX_HX711_ERRORS) {
                fillEnd("hx711_error", dispensed, elapsed);
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

        Serial.printf("%lu,%.2f,%.2f\n", (unsigned long)elapsed, filtered, dispensed);

        // No-progress check
        if (fabsf(filtered - lastWeight) < MIN_PROGRESS_G) {
            if (++noProgressCnt >= NO_PROGRESS_LIMIT) {
                fillEnd("empty_or_blocked", dispensed, elapsed);
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
                fillEnd("pump_failure", dispensed, elapsed);
                return;
            }
        }

        if (dispensed >= stopAt) {
            fillEnd("target_reached", dispensed, elapsed);
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
    Serial.println("BarBot scale ESP32");
    scale.begin();
    Serial.println("Commands: G3  G3.1  G3.2 W{g}  G3.3 F{cpg}  G3.4 N{n}  G4 I{n} W{g}");
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
}
