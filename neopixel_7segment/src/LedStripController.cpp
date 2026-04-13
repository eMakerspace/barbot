#include "LedStripController.h"
#include <Arduino.h>
#include <math.h>

// ============================================================================
// Phase tables — one per segment group.
// To add a new animation:
//   1. Add LED_DUR_<NAME> in config.h
//   2. Add the duration to the appropriate *_PHASES[] array
//   3. Add a case in the appropriate update*() dispatcher
//   4. Implement the anim<Name>(uint32_t t) function
// ============================================================================

static constexpr uint32_t BAR_PHASES[] = {
    cfg::LED_DUR_RAINBOW_WAVE,
    cfg::LED_DUR_COMET,
    cfg::LED_DUR_TWINKLE,
    cfg::LED_DUR_BREATHING,
    cfg::LED_DUR_THEATER_CHASE,
    cfg::LED_DUR_COLOR_WIPE,
    cfg::LED_DUR_SCANNER,
};
static constexpr int NUM_BAR = sizeof(BAR_PHASES) / sizeof(BAR_PHASES[0]);

static constexpr uint32_t RING_PHASES[] = {
    cfg::LED_DUR_RING_SPIN,
    cfg::LED_DUR_RING_PULSE,
    cfg::LED_DUR_RING_CYLON,
};
static constexpr int NUM_RING = sizeof(RING_PHASES) / sizeof(RING_PHASES[0]);

static constexpr uint32_t U_PHASES[] = {
    cfg::LED_DUR_U_FILL,
    cfg::LED_DUR_U_BOUNCE,
    cfg::LED_DUR_U_METEOR,
};
static constexpr int NUM_U = sizeof(U_PHASES) / sizeof(U_PHASES[0]);

// ============================================================================
// Public API
// ============================================================================

void LedStripController::begin() {
    FastLED.addLeds<WS2812, cfg::LED_PIN, GRB>(leds_, cfg::LED_COUNT);
    FastLED.setBrightness(200);
    FastLED.clear(true);

    uint32_t now = millis();
    bar_.phase = 0;  bar_.phaseStart = now;
    ring_.phase = 0; ring_.phaseStart = now;
    u_.phase = 0;    u_.phaseStart = now;
    lastFrame_ = 0;
}

void LedStripController::tick() {
    uint32_t now = millis();
    if (now - lastFrame_ < cfg::LED_FRAME_MS) return;

    switch (mode_) {
        case Mode::Idle: updateIdle(); break;
        case Mode::Off:
            FastLED.clear();
            show();
            break;
    }
}

void LedStripController::setIdle() {
    mode_ = Mode::Idle;
    uint32_t now = millis();
    bar_.phase = 0;  bar_.phaseStart = now;
    ring_.phase = 0; ring_.phaseStart = now;
    u_.phase = 0;    u_.phaseStart = now;
}

void LedStripController::setOff() {
    mode_ = Mode::Off;
}

// ============================================================================
// Internal helpers
// ============================================================================

void LedStripController::show() {
    FastLED.show();
    lastFrame_ = millis();
}

uint8_t LedStripController::gamma8(uint8_t v) {
    return (uint8_t)((uint32_t)v * v / 255);
}

uint8_t LedStripController::sinBright(uint32_t t, uint32_t period,
                                       uint8_t lo, uint8_t hi) {
    float angle = 2.0f * 3.14159f * (float)(t % period) / (float)period;
    float s = 0.5f + 0.5f * sinf(angle);
    return gamma8((uint8_t)(lo + s * (hi - lo)));
}

// Advance a SegState to the next random phase, avoiding repeats.
static void advancePhase(SegState &seg, uint32_t now,
                          const uint32_t *durations, int numPhases) {
    int next;
    do { next = random(0, numPhases); } while (next == seg.phase && numPhases > 1);
    seg.phase = next;
    seg.phaseStart = now;
}

// ============================================================================
// Idle animation dispatchers — all three segments update every frame
// ============================================================================

void LedStripController::updateIdle() {
    uint32_t now = millis();

    // --- BAR ---
    {
        uint32_t t = now - bar_.phaseStart;
        if (t >= BAR_PHASES[bar_.phase]) {
            advancePhase(bar_, now, BAR_PHASES, NUM_BAR);
            t = 0;
        }
        switch (bar_.phase) {
            case 0: animRainbowWave  (t); break;
            case 1: animComet        (t); break;
            case 2: animTwinkle      (t); break;
            case 3: animBreathing    (t); break;
            case 4: animTheaterChase (t); break;
            case 5: animColorWipe    (t); break;
            case 6: animScanner      (t); break;
        }
    }

    // --- RING ---
    {
        uint32_t t = now - ring_.phaseStart;
        if (t >= RING_PHASES[ring_.phase]) {
            advancePhase(ring_, now, RING_PHASES, NUM_RING);
            t = 0;
        }
        switch (ring_.phase) {
            case 0: animRingSpin  (t); break;
            case 1: animRingPulse (t); break;
            case 2: animRingCylon (t); break;
        }
    }

    // --- U-SHAPE ---
    {
        uint32_t t = now - u_.phaseStart;
        if (t >= U_PHASES[u_.phase]) {
            advancePhase(u_, now, U_PHASES, NUM_U);
            t = 0;
        }
        switch (u_.phase) {
            case 0: animUFill   (t); break;
            case 1: animUBounce (t); break;
            case 2: animUMeteor (t); break;
        }
    }

    show();
}

// ============================================================================
// BAR animations  (write only to leds_[BAR_START … BAR_START+BAR_LEN-1])
// ============================================================================

// Phase 0 – Rainbow wave
// A smooth hue gradient shifts along the bar like a flowing rainbow.
// Period: 4 000 ms for one full rotation.
void LedStripController::animRainbowWave(uint32_t t) {
    constexpr uint32_t PERIOD = 4000;
    uint8_t startHue = (uint8_t)(255UL * (t % PERIOD) / PERIOD);
    for (uint16_t i = 0; i < cfg::BAR_LEN; i++) {
        uint8_t hue = startHue + (uint8_t)(255UL * i / cfg::BAR_LEN);
        leds_[cfg::BAR_START + i] = CHSV(hue, 255, 220);
    }
}

// Phase 1 – Comet
// A bright head races along the bar leaving a decaying tail.
void LedStripController::animComet(uint32_t t) {
    constexpr uint16_t TAIL   = 12;
    constexpr uint32_t PERIOD = (uint32_t)cfg::BAR_LEN * cfg::LED_FRAME_MS;

    for (uint16_t i = 0; i < cfg::BAR_LEN; i++) {
        leds_[cfg::BAR_START + i].nscale8(210);
    }

    uint16_t head = (uint16_t)((t / cfg::LED_FRAME_MS) % cfg::BAR_LEN);
    leds_[cfg::BAR_START + head] = CRGB::White;

    for (uint16_t j = 1; j < TAIL && j <= head; j++) {
        uint8_t bright = gamma8((uint8_t)(255 - 255 * j / TAIL));
        leds_[cfg::BAR_START + head - j] = CRGB(bright, bright, bright);
    }
    (void)PERIOD;
}

// Phase 2 – Twinkle
// Random pixels flash to random colours and fade back to black.
void LedStripController::animTwinkle(uint32_t t) {
    (void)t;
    for (uint16_t i = 0; i < cfg::BAR_LEN; i++) {
        leds_[cfg::BAR_START + i].nscale8(230);
    }
    constexpr int SPARKS = 4;
    for (int s = 0; s < SPARKS; s++) {
        uint16_t idx = random16(cfg::BAR_LEN);
        leds_[cfg::BAR_START + idx] = CHSV(random8(), 200, 255);
    }
}

// Phase 3 – Breathing
// The entire bar breathes between deep-blue and bright-cyan. Cycle: 3 000 ms.
void LedStripController::animBreathing(uint32_t t) {
    constexpr uint32_t PERIOD = 3000;
    uint8_t bright = sinBright(t, PERIOD, 5, 255);
    CHSV color(145, 220, bright);
    for (uint16_t i = 0; i < cfg::BAR_LEN; i++) {
        leds_[cfg::BAR_START + i] = color;
    }
}

// Phase 4 – Theater chase
// Every-3rd-pixel chase with slowly rotating hue.
void LedStripController::animTheaterChase(uint32_t t) {
    constexpr uint32_t STEP_MS    = 120;
    constexpr uint32_t HUE_PERIOD = cfg::LED_DUR_THEATER_CHASE;
    uint8_t hue    = (uint8_t)(255UL * (t % HUE_PERIOD) / HUE_PERIOD);
    int     offset = (t / STEP_MS) % 3;

    for (uint16_t i = 0; i < cfg::BAR_LEN; i++) {
        leds_[cfg::BAR_START + i] = ((i % 3) == (uint16_t)offset)
                                     ? CRGB(CHSV(hue, 255, 200))
                                     : CRGB::Black;
    }
}

// Phase 5 – Colour wipe
// Fills pixel-by-pixel with one colour, then clears. Cycles R→G→B.
void LedStripController::animColorWipe(uint32_t t) {
    constexpr uint32_t MS_PER_PIX = 15;
    constexpr uint32_t HALF  = (uint32_t)cfg::BAR_LEN * MS_PER_PIX;
    constexpr uint32_t CYCLE = HALF * 2;
    constexpr uint8_t  HUES[3] = { 0, 96, 160 };

    uint32_t pos    = t % CYCLE;
    uint8_t  hue    = HUES[(t / CYCLE) % 3];
    bool     wiping = (pos < HALF);
    uint16_t pixel  = (uint16_t)(pos / MS_PER_PIX);
    if (pixel >= cfg::BAR_LEN) pixel = cfg::BAR_LEN - 1;

    for (uint16_t i = 0; i < cfg::BAR_LEN; i++) {
        bool filled = wiping ? (i <= pixel) : (i > pixel);
        leds_[cfg::BAR_START + i] = filled ? CRGB(CHSV(hue, 255, 200)) : CRGB::Black;
    }
}

// Phase 6 – Scanner (KITT-style)
// A single bright pixel bounces back and forth with a dim trail.
void LedStripController::animScanner(uint32_t t) {
    constexpr uint32_t STEP_MS    = 18;
    constexpr uint16_t TRAIL      = 6;
    constexpr uint32_t HUE_PERIOD = 6000;

    uint8_t  hue   = (uint8_t)(255UL * (t % HUE_PERIOD) / HUE_PERIOD);
    uint32_t cycle = (uint32_t)(cfg::BAR_LEN - 1) * 2;
    uint16_t raw   = (uint16_t)((t / STEP_MS) % cycle);
    uint16_t pos   = (raw < cfg::BAR_LEN) ? raw : (uint16_t)(cycle - raw);

    for (uint16_t i = 0; i < cfg::BAR_LEN; i++) {
        uint16_t dist = (i > pos) ? (i - pos) : (pos - i);
        if (dist == 0) {
            leds_[cfg::BAR_START + i] = CHSV(hue, 255, 255);
        } else if (dist < TRAIL) {
            uint8_t v = gamma8((uint8_t)(255 - 255 * dist / TRAIL));
            leds_[cfg::BAR_START + i] = CHSV(hue, 255, v);
        } else {
            leds_[cfg::BAR_START + i] = CRGB::Black;
        }
    }
}

// ============================================================================
// RING animations  (write only to leds_[RING_START … RING_START+RING_LEN-1])
// ============================================================================

// Phase 7 – Ring spin
// A bright arc of ~5 LEDs rotates continuously around the ring.
// One full revolution: 1 500 ms. Hue slowly changes over 8 000 ms.
void LedStripController::animRingSpin(uint32_t t) {
    constexpr uint32_t REV_MS     = 1500;
    constexpr uint32_t HUE_PERIOD = 8000;
    constexpr uint16_t ARC        = 5;    // lit arc width

    uint8_t  hue  = (uint8_t)(255UL * (t % HUE_PERIOD) / HUE_PERIOD);
    uint16_t head = (uint16_t)((uint32_t)cfg::RING_LEN * (t % REV_MS) / REV_MS);

    for (uint16_t i = 0; i < cfg::RING_LEN; i++) {
        // Angular distance from head (wrapping)
        uint16_t dist = (uint16_t)((i + cfg::RING_LEN - head) % cfg::RING_LEN);
        if (dist < ARC) {
            uint8_t v = gamma8((uint8_t)(255 - 255 * dist / ARC));
            leds_[cfg::RING_START + i] = CHSV(hue, 255, v);
        } else {
            leds_[cfg::RING_START + i] = CRGB::Black;
        }
    }
}

// Phase 8 – Ring pulse
// The whole ring breathes with a slowly rotating hue — emphasising its
// circular nature by pulsing all pixels uniformly. Cycle: 2 500 ms.
void LedStripController::animRingPulse(uint32_t t) {
    constexpr uint32_t BREATH_PERIOD = 2500;
    constexpr uint32_t HUE_PERIOD    = 12000;

    uint8_t hue    = (uint8_t)(255UL * (t % HUE_PERIOD) / HUE_PERIOD);
    uint8_t bright = sinBright(t, BREATH_PERIOD, 10, 255);

    for (uint16_t i = 0; i < cfg::RING_LEN; i++) {
        leds_[cfg::RING_START + i] = CHSV(hue, 220, bright);
    }
}

// Phase 9 – Ring cylon
// A single bright dot sweeps around the ring continuously (no bounce —
// purely circular), leaving a short tail. Revolution: 1 200 ms.
void LedStripController::animRingCylon(uint32_t t) {
    constexpr uint32_t REV_MS = 1200;
    constexpr uint16_t TAIL   = 4;

    uint16_t head = (uint16_t)((uint32_t)cfg::RING_LEN * (t % REV_MS) / REV_MS);

    for (uint16_t i = 0; i < cfg::RING_LEN; i++) {
        uint16_t dist = (uint16_t)((i + cfg::RING_LEN - head) % cfg::RING_LEN);
        if (dist == 0) {
            leds_[cfg::RING_START + i] = CRGB::White;
        } else if (dist < TAIL) {
            uint8_t v = gamma8((uint8_t)(255 - 255 * dist / TAIL));
            leds_[cfg::RING_START + i] = CRGB(v, v, v);
        } else {
            leds_[cfg::RING_START + i] = CRGB::Black;
        }
    }
}

// ============================================================================
// U-SHAPE animations  (write only to leds_[U_START … U_START+U_LEN-1])
//
// U physical layout (28 LEDs):
//   Index 0 = top of left leg
//   Index 13 = bottom of U (corner)
//   Index 27 = top of right leg
//   The path is linear: 0 → 13 → 27
// ============================================================================

// Phase 10 – U fill
// Fills symmetrically from both tops toward the bottom, then clears.
// Both legs advance at the same rate so the U "fills up" evenly.
void LedStripController::animUFill(uint32_t t) {
    constexpr uint32_t MS_PER_PIX = 30;
    // Each leg is half the U length
    constexpr uint16_t LEG = cfg::U_LEN / 2;          // 14
    constexpr uint32_t HALF  = (uint32_t)LEG * MS_PER_PIX;
    constexpr uint32_t CYCLE = HALF * 2;
    constexpr uint8_t  HUES[3] = { 0, 96, 160 };

    uint32_t pos    = t % CYCLE;
    uint8_t  hue    = HUES[(t / CYCLE) % 3];
    bool     filling = (pos < HALF);
    uint16_t depth   = (uint16_t)(pos / MS_PER_PIX);  // 0..LEG
    if (depth >= LEG) depth = LEG;

    for (uint16_t i = 0; i < cfg::U_LEN; i++) {
        // Left leg: index 0 (top) → LEG-1 (bottom-left)
        // Right leg: index LEG (bottom-right) → U_LEN-1 (top)
        bool inLeftLeg  = (i < LEG);
        uint16_t legPos = inLeftLeg ? i : (cfg::U_LEN - 1 - i);  // distance from top
        bool lit = filling ? (legPos < depth) : (legPos >= depth);
        leds_[cfg::U_START + i] = lit ? CRGB(CHSV(hue, 255, 200)) : CRGB::Black;
    }
}

// Phase 11 – U bounce
// A bright comet travels down the left leg, around the bottom, and back up
// the right leg, then reverses — like a ball bouncing in the U.
void LedStripController::animUBounce(uint32_t t) {
    constexpr uint32_t STEP_MS = 20;
    constexpr uint16_t TAIL    = 5;
    constexpr uint32_t HUE_PERIOD = 6000;

    uint8_t  hue   = (uint8_t)(255UL * (t % HUE_PERIOD) / HUE_PERIOD);
    uint32_t cycle = (uint32_t)(cfg::U_LEN - 1) * 2;
    uint16_t raw   = (uint16_t)((t / STEP_MS) % cycle);
    // Triangle wave: 0 → U_LEN-1 → 0
    uint16_t pos   = (raw < cfg::U_LEN) ? raw : (uint16_t)(cycle - raw);

    for (uint16_t i = 0; i < cfg::U_LEN; i++) {
        uint16_t dist = (i > pos) ? (i - pos) : (pos - i);
        if (dist == 0) {
            leds_[cfg::U_START + i] = CHSV(hue, 255, 255);
        } else if (dist < TAIL) {
            uint8_t v = gamma8((uint8_t)(255 - 255 * dist / TAIL));
            leds_[cfg::U_START + i] = CHSV(hue, 200, v);
        } else {
            leds_[cfg::U_START + i] = CRGB::Black;
        }
    }
}

// Phase 12 – U meteor
// Two meteors travel simultaneously down each leg toward the bottom.
// They meet at the base, creating a V-convergence effect, then restart.
// Speed: one LED per STEP_MS ms.
void LedStripController::animUMeteor(uint32_t t) {
    constexpr uint32_t STEP_MS = 25;
    constexpr uint16_t TAIL    = 4;
    constexpr uint16_t LEG     = cfg::U_LEN / 2;  // 14

    // Fade all U pixels slightly each frame
    for (uint16_t i = 0; i < cfg::U_LEN; i++) {
        leds_[cfg::U_START + i].nscale8(200);
    }

    // Both meteors advance in leg-position (0 = top, LEG-1 = bottom)
    uint16_t pos = (uint16_t)((t / STEP_MS) % LEG);

    // Left leg: physical index 0 (top) … LEG-1 (bottom)
    leds_[cfg::U_START + pos] = CRGB::White;
    for (uint16_t j = 1; j < TAIL && j <= pos; j++) {
        uint8_t v = gamma8((uint8_t)(255 - 255 * j / TAIL));
        leds_[cfg::U_START + pos - j] = CRGB(v, v, v);
    }

    // Right leg: physical index U_LEN-1 (top) … LEG (bottom)
    uint16_t rightIdx = cfg::U_LEN - 1 - pos;
    leds_[cfg::U_START + rightIdx] = CRGB::White;
    for (uint16_t j = 1; j < TAIL && j <= pos; j++) {
        uint8_t v = gamma8((uint8_t)(255 - 255 * j / TAIL));
        leds_[cfg::U_START + rightIdx + j] = CRGB(v, v, v);
    }
}
