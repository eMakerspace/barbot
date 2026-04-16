#include "DisplayController.h"
#include <Arduino.h>
#include <math.h>

// ---------------------------------------------------------------------------
// Segment bitmasks  (bit 0 = a … bit 6 = g)
// ---------------------------------------------------------------------------
//   ─a─
//  f   b
//   ─g─
//  e   c
//   ─d─
static constexpr uint8_t SEG_A = 0x01;
static constexpr uint8_t SEG_B = 0x02;
static constexpr uint8_t SEG_C = 0x04;
static constexpr uint8_t SEG_D = 0x08;
static constexpr uint8_t SEG_E = 0x10;
static constexpr uint8_t SEG_F = 0x20;
static constexpr uint8_t SEG_G = 0x40;
static constexpr uint8_t SEG_ALL = 0x7F;

// Clockwise outer-ring order: a → b → c → d → e → f
static constexpr uint8_t RING[6] = { SEG_A, SEG_B, SEG_C, SEG_D, SEG_E, SEG_F };

// ============================================================================
// ANIMATION PHASE DURATIONS  (ms) — EDIT HERE TO CUSTOMIZE
// ============================================================================
// Each value below controls how long a specific animation phase lasts.
// Total cycle time is automatically summed from these values.
// To disable a phase, set its duration to 0 and add it back elsewhere.
//
constexpr uint32_t PHASE_DUR_SPINNER_SYNC      = 25000;
constexpr uint32_t PHASE_DUR_SPINNER_OPPOSED   = 20000;
constexpr uint32_t PHASE_DUR_PERIMETER_CHASE   = 20000;
constexpr uint32_t PHASE_DUR_BREATHING_BARS    = 30000;
constexpr uint32_t PHASE_DUR_RISE_FROM_BOTTOM  = 25000;
constexpr uint32_t PHASE_DUR_FALL_FROM_TOP     = 25000;
constexpr uint32_t PHASE_DUR_ALTERNATING_PULSE = 20000;
constexpr uint32_t PHASE_DUR_HEARTBEAT         = 30000;
constexpr uint32_t PHASE_DUR_SEGMENT_RAIN      = 25000;
constexpr uint32_t PHASE_DUR_SPLIT_MIRROR      = 25000;
constexpr uint32_t PHASE_DUR_BOUNCING_BAR      = 25000;
constexpr uint32_t PHASE_DUR_EXPLODE_IMPLODE   = 25000;
constexpr uint32_t PHASE_DUR_PINGPONG_WAVE     = 30000;
constexpr uint32_t PHASE_DUR_STROBE_ACCEL      = 20000;
constexpr uint32_t PHASE_DUR_SEGMENT_MORPH     = 30000;
constexpr uint32_t PHASE_DUR_CASCADE_ACROSS    = 30000;
constexpr uint32_t PHASE_DUR_SNAKE             = 35000;  // NEW: snake moving around perimeter
constexpr uint32_t PHASE_DUR_SNAKE_EATS_DOT    = 30000;  // NEW: 3-link snake chases 1-link prey
// ============================================================================

// Assemble durations array
static constexpr uint32_t PHASE_DUR[] = {
    PHASE_DUR_SPINNER_SYNC,
    PHASE_DUR_SPINNER_OPPOSED,
    PHASE_DUR_PERIMETER_CHASE,
    PHASE_DUR_BREATHING_BARS,
    PHASE_DUR_RISE_FROM_BOTTOM,
    PHASE_DUR_FALL_FROM_TOP,
    PHASE_DUR_ALTERNATING_PULSE,
    PHASE_DUR_HEARTBEAT,
    PHASE_DUR_SEGMENT_RAIN,
    PHASE_DUR_SPLIT_MIRROR,
    PHASE_DUR_BOUNCING_BAR,
    PHASE_DUR_EXPLODE_IMPLODE,
    PHASE_DUR_PINGPONG_WAVE,
    PHASE_DUR_STROBE_ACCEL,
    PHASE_DUR_SEGMENT_MORPH,
    PHASE_DUR_CASCADE_ACROSS,
    PHASE_DUR_SNAKE,
    PHASE_DUR_SNAKE_EATS_DOT,
};
static constexpr int NUM_PHASES = 18;
// Total cycle: sum of all durations above ≈ 470 s = 7 min 50 s
static constexpr uint32_t TOTAL_CYCLE_MS = 470000;

// ---------------------------------------------------------------------------
// Public mode setters
// ---------------------------------------------------------------------------
void DisplayController::setStatic(uint8_t v) {
    mode_         = Mode::Static;
    displayValue_ = v % 100;
    modeStart_    = millis();
    updateStatic();
}

void DisplayController::setBlinking(uint8_t v) {
    mode_         = Mode::Blinking;
    displayValue_ = v % 100;
    modeStart_    = millis();
}

void DisplayController::setBreathing(uint8_t v) {
    mode_         = Mode::Breathing;
    displayValue_ = v % 100;
    modeStart_    = millis();
}

void DisplayController::setAnimating() {
    mode_       = Mode::Animating;
    modeStart_  = millis();
    phase_      = random(0, NUM_PHASES);  // start with a random phase
    phaseStart_ = modeStart_;
}

void DisplayController::setCup() {
    mode_      = Mode::Cup;
    modeStart_ = millis();
}

void DisplayController::setCounting(uint32_t takt_ms) {
    mode_      = Mode::Counting;
    taktMs_    = takt_ms;
    modeStart_ = millis();
}

void DisplayController::setDrinkReady(uint8_t v) {
    mode_         = Mode::DrinkReady;
    displayValue_ = v % 100;
    modeStart_    = millis();
}

// ---------------------------------------------------------------------------
// Main tick
// ---------------------------------------------------------------------------
void DisplayController::tick() {
    switch (mode_) {
        case Mode::Static:     updateStatic();     break;
        case Mode::Blinking:   updateBlinking();   break;
        case Mode::Breathing:  updateBreathing();  break;
        case Mode::Animating:  updateAnimating();  break;
        case Mode::Cup:        updateCup();        break;
        case Mode::Counting:   updateCounting();   break;
        case Mode::DrinkReady: updateDrinkReady(); break;
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
uint8_t DisplayController::gamma(uint8_t v) {
    return (uint8_t)((uint32_t)v * v / 255);
}

uint8_t DisplayController::sinBright(uint32_t t, uint32_t period,
                                     uint8_t minBr, uint8_t maxBr) {
    float angle = 2.0f * 3.14159f * (float)(t % period) / (float)period;
    float s = 0.5f + 0.5f * sinf(angle);              // 0.0 → 1.0
    uint8_t v = (uint8_t)(minBr + s * (maxBr - minBr));
    return gamma(v);
}

void DisplayController::show(uint8_t onesMask, uint8_t tensMask,
                              uint8_t onesBr, uint8_t tensBr) {
    driver_.setRawMaskOnes(onesMask);
    driver_.setRawMaskTens(tensMask);
    driver_.setBrightnessOnes(onesBr);
    driver_.setBrightnessTens(tensBr);
}

// ---------------------------------------------------------------------------
// Static / Blinking / Breathing
// ---------------------------------------------------------------------------
void DisplayController::updateStatic() {
    driver_.setOnes(displayValue_ % 10);
    driver_.setTens(displayValue_ / 10);
    driver_.setBrightnessOnes(255);
    driver_.setBrightnessTens(255);
}

void DisplayController::updateBlinking() {
    uint32_t phase = (millis() - modeStart_) % 800;
    uint8_t  br    = (phase < 400) ? 255 : 0;
    driver_.setOnes(displayValue_ % 10);
    driver_.setTens(displayValue_ / 10);
    driver_.setBrightnessOnes(br);
    driver_.setBrightnessTens(br);
}

void DisplayController::updateBreathing() {
    uint32_t t  = millis() - modeStart_;
    uint8_t  br = sinBright(t, 4000, 1, 255);
    driver_.setOnes(displayValue_ % 10);
    driver_.setTens(displayValue_ / 10);
    driver_.setBrightnessOnes(br);
    driver_.setBrightnessTens(br);
}

// ---------------------------------------------------------------------------
// Cup animation  (-c)
//
// One 5-second cycle on the ones display only:
//   0–300 ms   blank                   (cup not yet in frame)
//   300–600 ms SEG_A (top bar)         cap of cup entering from above
//   600–900 ms SEG_G (middle bar)      cup body falling
//   900 ms     U-shape (b+c+d+e+f)     cup lands — rapid flash on impact
//   900–1000   landing flash (PWM)
//   1000–3000  hold U-shape
// ---------------------------------------------------------------------------
// Counting: 00 → 99 → 00, one step every taktMs_
// Steps 0–99 count up; steps 100–198 count back down.
// Total cycle = 199 × taktMs_ (at 400 ms: ~79.6 s)
// ---------------------------------------------------------------------------
void DisplayController::updateCounting() {
    uint32_t t    = millis() - modeStart_;
    uint32_t step = (t / taktMs_) % 199;   // 0..198 ping-pong
    uint8_t  val  = (step < 100) ? (uint8_t)step : (uint8_t)(198 - step);
    driver_.setOnes(val % 10);
    driver_.setTens(val / 10);
    driver_.setBrightnessOnes(255);
    driver_.setBrightnessTens(255);
}

//   3000–4500  U-shape fades out (PWM)
//   4500–5000  blank pause
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// DrinkReady: order number blinks slow→fast over a 5s cycle, looping.
// Period ramps from 800ms down to 80ms linearly within each cycle.
// ---------------------------------------------------------------------------
void DisplayController::updateDrinkReady() {
    uint32_t t  = millis() - modeStart_;
    uint32_t pt = t % 5000;  // position within 5s cycle

    // Linear ramp: 800ms period at start → 80ms at end of cycle
    uint32_t period = 800 - (uint32_t)(720UL * pt / 5000);
    if (period < 80) period = 80;

    uint8_t br = (t % period < period / 2) ? 255 : 0;
    driver_.setOnes(displayValue_ % 10);
    driver_.setTens(displayValue_ / 10);
    driver_.setBrightnessOnes(br);
    driver_.setBrightnessTens(br);
}

void DisplayController::updateCup() {
    constexpr uint32_t CYCLE = 5000;
    uint32_t t = (millis() - modeStart_) % CYCLE;

    // Cup at different heights during fall
    constexpr uint8_t CUP_MIDDLE = SEG_F | SEG_G | SEG_B;  // f+g+b at middle row
    constexpr uint8_t CUP_BOTTOM = SEG_E | SEG_D | SEG_C;  // e+d+c at bottom row

    // Tens display stays blank throughout
    driver_.setRawMaskTens(0x00);
    driver_.setBrightnessTens(0);

    if (t < 100) {
        // Pre-entry blank
        show(0x00, 0x00, 0, 0);

    } else if (t < 200) {
        // Cup entering: just a single segment (a)
        show(SEG_A, 0x00);

    } else if (t < 300) {
        // Cup falling through middle row: f+g+b
        show(CUP_MIDDLE, 0x00);

    } else if (t < 800) {
        // Impact flash: rapid PWM oscillation to simulate bounce/shock
        // 25 ms on / 25 ms off → 4 flashes in 100 ms
        uint8_t flash = ((t - 300) % 50 < 25) ? 255 : 50;
        show(CUP_BOTTOM, 0x00, flash, 0);

    } else if (t < 3000) {
        // Cup resting — held at full brightness
        show(CUP_BOTTOM, 0x00);

    } else if (t < 4500) {
        // Fade out: linear decay over 1500 ms
        uint8_t br = (uint8_t)(255UL * (4500 - t) / 1500);
        show(CUP_BOTTOM, 0x00, gamma(br), 0);

    } else {
        // Blank pause before next cycle
        show(0x00, 0x00, 0, 0);
    }
}

// ---------------------------------------------------------------------------
// Idle animation dispatcher  (-i)
//
// Advances through NUM_PHASES phases sequentially, each with its own duration.
// After all phases complete the full cycle repeats (~6 min 45 s).
// ---------------------------------------------------------------------------
void DisplayController::updateAnimating() {
    uint32_t now  = millis();
    uint32_t t    = now - phaseStart_;   // time within current phase

    // Advance to next phase (random) when the current one expires
    while (t >= PHASE_DUR[phase_]) {
        t          -= PHASE_DUR[phase_];
        phaseStart_ = now - t;
        // Pick a random next phase (avoid repeating the same phase back-to-back)
        int nextPhase;
        do {
            nextPhase = random(0, NUM_PHASES);
        } while (nextPhase == phase_);
        phase_ = nextPhase;
    }

    switch (phase_) {
        case  0: phaseSpinnerSync      (t); break;
        case  1: phaseSpinnerOpposed   (t); break;
        case  2: phasePerimeterChase   (t); break;
        case  3: phaseBreathingBars    (t); break;
        case  4: phaseRiseFromBottom   (t); break;
        case  5: phaseFallFromTop      (t); break;
        case  6: phaseAlternatingPulse (t); break;
        case  7: phaseHeartbeat        (t); break;
        case  8: phaseSegmentRain      (t); break;
        case  9: phaseSplitMirror      (t); break;
        case 10: phaseBouncingBar      (t); break;
        case 11: phaseExplodeImplode   (t); break;
        case 12: phasePingPongWave     (t); break;
        case 13: phaseStrobeAccel      (t); break;
        case 14: phaseSegmentMorph     (t); break;
        case 15: phaseCascadeAcross    (t); break;
        case 16: phaseSnake            (t); break;
        case 17: phaseSnakeEatsDot     (t); break;
    }
}

// ===========================================================================
// Animation phase implementations
// ===========================================================================

// Phase 0 – Spinner sync  (25 s)
// Single-segment "loading" spinner clockwise on both displays, same step.
// Two-segment trail version looks smoother.
void DisplayController::phaseSpinnerSync(uint32_t t) {
    int step = (t / 80) % 6;
    // Two adjacent segments lit (head + trail)
    uint8_t mask = RING[step] | RING[(step + 5) % 6];  // head + trailing segment
    show(mask, mask);
}

// Phase 1 – Spinner opposed  (20 s)
// Ones clockwise, tens counterclockwise — they sweep towards / away from each other.
void DisplayController::phaseSpinnerOpposed(uint32_t t) {
    int fwd = (t / 80) % 6;
    int rev = (5 - fwd + 6) % 6;
    uint8_t maskFwd = RING[fwd] | RING[(fwd + 5) % 6];
    uint8_t maskRev = RING[rev] | RING[(rev + 5) % 6];
    show(maskFwd, maskRev);
}

// Phase 2 – Perimeter chase with offset  (20 s)
// Ones leads tens by two steps; looks like segments chasing around a track.
void DisplayController::phasePerimeterChase(uint32_t t) {
    int step  = (t / 100) % 6;
    int stepB = (step + 2) % 6;
    show(RING[step], RING[stepB]);
}

// Phase 3 – Breathing bars  (30 s)  ← uses PWM
// Three horizontal bars on both displays. Ones breathes with a sine wave,
// tens breathes 180° out of phase — creates a visual "seesaw" effect.
void DisplayController::phaseBreathingBars(uint32_t t) {
    constexpr uint8_t BARS = SEG_A | SEG_G | SEG_D;   // top, middle, bottom bars
    constexpr uint32_t PERIOD = 3000;
    uint8_t br1 = sinBright(t,          PERIOD, 10, 255);
    uint8_t br2 = sinBright(t + PERIOD/2, PERIOD, 10, 255);  // 180° offset
    show(BARS, BARS, br1, br2);
}

// Phase 4 – Rise from bottom  (25 s)
// Segments build upward from d, then collapse back down.
// Both displays same pattern; tens lags ones by 2 steps.
void DisplayController::phaseRiseFromBottom(uint32_t t) {
    static constexpr uint8_t FRAMES[] = {
        SEG_D,
        SEG_D | SEG_E | SEG_C,
        SEG_D | SEG_E | SEG_C | SEG_G,
        SEG_D | SEG_E | SEG_C | SEG_G | SEG_F | SEG_B,
        SEG_ALL,
        SEG_D | SEG_E | SEG_C | SEG_G | SEG_F | SEG_B,
        SEG_D | SEG_E | SEG_C | SEG_G,
        SEG_D | SEG_E | SEG_C,
        SEG_D,
        0x00,
    };
    static constexpr int N = 10;
    int step  = (t / 300) % (N * 2);      // forward then backward
    int frame = (step < N) ? step : (2 * N - 1 - step);
    if (frame < 0) frame = 0;
    int frameB = (frame + N - 2) % N;     // tens lags by 2
    show(FRAMES[frame], FRAMES[frameB]);
}

// Phase 5 – Fall from top  (25 s)
// Mirror of phase 4: segments build downward from a, then collapse upward.
void DisplayController::phaseFallFromTop(uint32_t t) {
    static constexpr uint8_t FRAMES[] = {
        SEG_A,
        SEG_A | SEG_F | SEG_B,
        SEG_A | SEG_F | SEG_B | SEG_G,
        SEG_A | SEG_F | SEG_B | SEG_G | SEG_E | SEG_C,
        SEG_ALL,
        SEG_A | SEG_F | SEG_B | SEG_G | SEG_E | SEG_C,
        SEG_A | SEG_F | SEG_B | SEG_G,
        SEG_A | SEG_F | SEG_B,
        SEG_A,
        0x00,
    };
    static constexpr int N = 10;
    int step  = (t / 300) % (N * 2);
    int frame = (step < N) ? step : (2 * N - 1 - step);
    if (frame < 0) frame = 0;
    show(FRAMES[frame], FRAMES[frame]);
}

// Phase 6 – Alternating pulse  (20 s)  ← uses PWM (variable period)
// Ones and tens take turns lighting up with three bars.
// Period accelerates from 600 ms → 80 ms → 600 ms over the phase duration.
void DisplayController::phaseAlternatingPulse(uint32_t t) {
    constexpr uint8_t BARS = SEG_A | SEG_G | SEG_D;
    constexpr uint32_t DUR = 20000;
    // Map time to [0,1] → [0,π] → period ramp (fast in middle, slow at ends)
    float pos    = (float)t / DUR;                    // 0 → 1
    float ramp   = sinf(3.14159f * pos);              // 0 → 1 → 0
    uint32_t per = (uint32_t)(600 - ramp * 520);      // 600 ms → 80 ms → 600 ms
    uint32_t slot = t % per;
    bool onesOn  = (slot < per / 2);
    show(BARS, BARS,
         onesOn ? 255 : 0,
         onesOn ? 0   : 255);
}

// Phase 7 – Heartbeat  (30 s)
// Double-pulse rhythm: quick beat, short gap, quiet beat, long pause.
// Cycle: ON(80) OFF(80) ON(80) OFF(760) = 1000 ms per beat.
void DisplayController::phaseHeartbeat(uint32_t t) {
    constexpr uint32_t BEAT = 1000;
    uint32_t slot = t % BEAT;
    uint8_t  br;
    if      (slot <  80)  br = 255;   // first beat (strong)
    else if (slot < 160)  br = 0;
    else if (slot < 240)  br = 140;   // second beat (softer)
    else                  br = 0;     // rest
    show(SEG_ALL, SEG_ALL, gamma(br), gamma(br));
}

// Phase 8 – Segment rain  (25 s)
// A single horizontal bar "falls" from a → g → d on ones;
// tens shows the opposite bar position (staggered visual).
void DisplayController::phaseSegmentRain(uint32_t t) {
    static constexpr uint8_t BARS[3] = { SEG_A, SEG_G, SEG_D };
    uint32_t slot  = t % 600;
    int      step  = (t / 600) % 3;
    int      stepB = (step + 1) % 3;
    // Brief blank between drops for clear separation
    uint8_t br = (slot < 500) ? 255 : 0;
    show(BARS[step], BARS[stepB], br, br);
}

// Phase 9 – Split mirror  (25 s)  ← uses PWM
// Left-side segments on ones, right-side on tens.
// They breathe in antiphase — looks like the display "opens" and "closes".
void DisplayController::phaseSplitMirror(uint32_t t) {
    constexpr uint8_t LEFT  = SEG_A | SEG_F | SEG_G | SEG_E | SEG_D;
    constexpr uint8_t RIGHT = SEG_A | SEG_B | SEG_G | SEG_C | SEG_D;
    constexpr uint32_t PERIOD = 3500;
    uint8_t br1 = sinBright(t,            PERIOD, 10, 255);
    uint8_t br2 = sinBright(t + PERIOD/2, PERIOD, 10, 255);
    show(LEFT, RIGHT, br1, br2);
}

// Phase 10 – Bouncing horizontal bar  (25 s)
// A single horizontal bar bounces: top → middle → bottom → middle → top.
// Ones and tens start half a period apart so one goes up while other goes down.
void DisplayController::phaseBouncingBar(uint32_t t) {
    static constexpr uint8_t BARS[3] = { SEG_A, SEG_G, SEG_D };
    // Triangle wave over 4 positions (0-1-2-1): 400 ms per step
    static constexpr int SEQ[4] = { 0, 1, 2, 1 };
    int step  = (t / 400) % 4;
    int stepB = (t / 400 + 2) % 4;   // half-period offset
    show(BARS[SEQ[step]], BARS[SEQ[stepB]]);
}

// Phase 11 – Explode / implode  (25 s)
// Starts from the middle segment, expands outward to fill all segments,
// then contracts back in. Repeats continuously.
// Both displays mirror each other.
void DisplayController::phaseExplodeImplode(uint32_t t) {
    static constexpr uint8_t FRAMES[] = {
        SEG_G,                                          // centre only
        SEG_G | SEG_A | SEG_D,                         // + top/bottom
        SEG_G | SEG_A | SEG_D | SEG_B | SEG_F,         // + outer verticals top
        SEG_ALL,                                        // fully lit
    };
    static constexpr int N = 4;
    // Step forward then backward (0-1-2-3-2-1): 250 ms per step
    int raw   = (t / 250) % (2 * N - 2);
    int frame = (raw < N) ? raw : (2 * N - 2 - raw);
    show(FRAMES[frame], FRAMES[frame]);
}

// Phase 12 – Ping-pong brightness wave  (30 s)  ← uses PWM
// Both displays show all segments. A sine brightness wave travels from
// ones to tens and back continuously — looks like light sloshing between them.
void DisplayController::phasePingPongWave(uint32_t t) {
    constexpr uint32_t PERIOD = 2000;
    uint8_t br1 = sinBright(t,            PERIOD, 5, 255);
    uint8_t br2 = sinBright(t + PERIOD/2, PERIOD, 5, 255);  // 180° offset
    show(SEG_ALL, SEG_ALL, br1, br2);
}

// Phase 13 – Strobe with acceleration  (20 s)  ← uses PWM
// All segments strobe. Period starts at 500 ms, accelerates to 50 ms at the
// midpoint, then decelerates back — creates a rush / wind-down feel.
void DisplayController::phaseStrobeAccel(uint32_t t) {
    constexpr uint32_t DUR = 20000;
    float pos    = (float)t / DUR;                   // 0 → 1
    float ramp   = sinf(3.14159f * pos);             // 0 → 1 → 0
    uint32_t per = (uint32_t)(500 - ramp * 450);     // 500 → 50 → 500 ms
    uint8_t  br  = (t % per < per / 2) ? 255 : 0;
    show(SEG_ALL, SEG_ALL, br, br);
}

// Phase 14 – Segment morph  (30 s)  ← uses PWM
// Cycles through five visually distinct segment patterns, 6 s each.
// Each pattern fades in with a sine ramp, holds, then fades out.
void DisplayController::phaseSegmentMorph(uint32_t t) {
    static constexpr uint8_t ONES_PAT[] = {
        SEG_A | SEG_G | SEG_D,           // three horizontal bars
        SEG_B | SEG_C | SEG_D | SEG_E | SEG_F,  // U-shape
        SEG_A | SEG_F | SEG_E | SEG_D | SEG_C | SEG_B,  // frame (no middle)
        SEG_A | SEG_D,                    // top + bottom only
        SEG_F | SEG_E | SEG_B | SEG_C,   // two vertical columns
    };
    static constexpr uint8_t TENS_PAT[] = {
        SEG_F | SEG_E | SEG_B | SEG_C,   // two vertical columns
        SEG_A | SEG_B | SEG_F,            // top cap ⌐
        SEG_G,                             // middle bar only
        SEG_A | SEG_G | SEG_D,           // three bars
        SEG_B | SEG_C | SEG_D | SEG_E | SEG_F,  // U-shape
    };
    int     patIdx = (t / 6000) % 5;
    uint32_t pt    = t % 6000;           // time within this pattern slot

    // Fade in: 0–1 s, hold: 1–4 s (with gentle sine breath), fade out: 4–6 s
    uint8_t br;
    if (pt < 1000) {
        br = gamma((uint8_t)(255 * pt / 1000));
    } else if (pt < 4000) {
        br = sinBright(pt, 3000, 140, 255);
    } else {
        br = gamma((uint8_t)(255 * (6000 - pt) / 2000));
    }
    show(ONES_PAT[patIdx], TENS_PAT[patIdx], br, br);
}

// Phase 15 – Cascade across  (30 s)
// A lit segment on the ones display "moves" to the tens display one step
// at a time, as if segments are flowing from left to right.
// Step duration: 200 ms; full transfer = 6 segments × 200 ms = 1.2 s cycle.
void DisplayController::phaseCascadeAcross(uint32_t t) {
    static constexpr uint8_t SEGS[7] = {
        SEG_A, SEG_B, SEG_C, SEG_D, SEG_E, SEG_F, SEG_G
    };
    int  step    = (t / 200) % 14;   // 7 segments × 2 (ones then tens)
    int  segIdx  = step % 7;
    bool onOnes  = (step < 7);
    show(onOnes ? SEGS[segIdx] : 0x00,
         onOnes ? 0x00 : SEGS[segIdx]);
}

// Phase 16 – Snake animation  (35 s)  ← snake moves across both displays
// A single snake traverses a continuous 12-segment path:
// positions 0-5 on ones display (a,b,c,d,e,f clockwise)
// positions 6-11 on tens display (a,b,c,d,e,f clockwise)
// Head is bright, body extends 4 segments behind with decreasing brightness.
// Creates a fluid motion flowing from ones display to tens and back.
void DisplayController::phaseSnake(uint32_t t) {
    // Segments in clockwise order
    static constexpr uint8_t SEGS[6] = { SEG_A, SEG_B, SEG_C, SEG_D, SEG_E, SEG_F };

    // Head position moves through all 12 positions (every 120 ms)
    int headPos = (t / 120) % 12;      // 0-11: covers both displays

    // Build body trail: head + 4 segments behind
    int pos[5];
    for (int i = 0; i < 5; i++) {
        pos[i] = (headPos - i + 12) % 12;
    }

    // Determine which segments light up on each display
    uint8_t maskOnes = 0, maskTens = 0;

    for (int i = 0; i < 5; i++) {
        int p = pos[i];
        if (p < 6) {
            // Segment is on ones display
            maskOnes |= SEGS[p];
        } else {
            // Segment is on tens display (positions 6-11 map to segments 0-5)
            maskTens |= SEGS[p - 6];
        }
    }

    // Overall brightness breathes gently to enhance motion perception
    uint8_t br = sinBright(t, 3500, 200, 255);
    show(maskOnes, maskTens, br, br);
}

// Phase 17 – Snake eats dot  (30 s)
// A 3-link snake (head + 2 body links) chases a 1-link dot on a continuous
// 12-position perimeter path (6 positions on ones + 6 on tens). When the
// head catches the dot, the dot respawns ahead and the chase repeats.
void DisplayController::phaseSnakeEatsDot(uint32_t t) {
    // Same clockwise perimeter mapping used by phaseSnake.
    static constexpr uint8_t SEGS[6] = { SEG_A, SEG_B, SEG_C, SEG_D, SEG_E, SEG_F };
    constexpr int PATH_LEN = 12;
    constexpr uint32_t STEP_MS = 140;
    constexpr int PREY_MOVE_EVERY = 3; // prey moves slower than snake

    int head = 0;
    int prey = 6; // start opposite side
    bool justAte = false;
    int steps = (int)(t / STEP_MS);

    for (int s = 0; s < steps; s++) {
        justAte = false;
        head = (head + 1) % PATH_LEN;

        if ((s % PREY_MOVE_EVERY) == 0) {
            prey = (prey + 1) % PATH_LEN;
        }

        if (head == prey) {
            // Respawn prey ahead of the snake so the next chase restarts quickly.
            prey = (head + 5 + (s % 3)) % PATH_LEN;
            justAte = true;
        }
    }

    int snake1 = (head - 1 + PATH_LEN) % PATH_LEN;
    int snake2 = (head - 2 + PATH_LEN) % PATH_LEN;

    uint8_t onesMask = 0;
    uint8_t tensMask = 0;

    auto addPos = [&](int pos) {
        if (pos < 6) {
            onesMask |= SEGS[pos];
        } else {
            tensMask |= SEGS[pos - 6];
        }
    };

    addPos(head);
    addPos(snake1);
    addPos(snake2);
    addPos(prey);

    // Quick flash right after an "eat" event to make the collision visible.
    uint8_t br = 255;
    if (justAte && ((t / 40) % 2 == 0)) {
        br = 40;
    }
    show(onesMask, tensMask, br, br);
}
