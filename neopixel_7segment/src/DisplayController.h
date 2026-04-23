#pragma once
#include <cstdint>
#include <cmath>
#include "SevenSegmentDriver.h"

// DisplayController manages display modes and drives all animations.
//
// All timing is millis()-based and non-blocking — tick() returns immediately
// every call. The display hardware is never touched directly; only the
// SevenSegmentDriver's volatile setters are called (safe across cores).
class DisplayController {
public:
    enum class Mode : uint8_t {
        Static,      // static number
        Blinking,    // number blinks on/off
        Breathing,   // number breathes (continuous sine fade)
        Animating,   // segment-only idle animations (~6.75 min cycle)
        Cup,         // falling-cup animation on ones display (5 s cycle)
        Counting,    // counts 00→99→00 at a fixed takt
        DrinkReady,  // escalating blink: slow→fast over 5s, repeating
        Working,     // work-in-progress animation: cyan/blue spinning
        EStop        // emergency stop: fast-blinking "E" on both digits
    };

    explicit DisplayController(SevenSegmentDriver &driver) : driver_(driver) {}

    void setStatic      (uint8_t value);
    void setBlinking    (uint8_t value);
    void setBreathing   (uint8_t value);
    void setAnimating   ();
    void setCup         ();
    void setCounting    (uint32_t takt_ms = 400);
    void setDrinkReady  (uint8_t value);
    void setWorking     ();
    void setEStop       ();

    // Call from loop() on every iteration to advance animation state.
    void tick();

private:
    SevenSegmentDriver &driver_;
    Mode     mode_          = Mode::Static;
    uint8_t  displayValue_  = 0;
    uint32_t modeStart_     = 0;   // millis() when current mode was set

    // Idle animation phase tracking
    int      phase_      = 0;      // current phase index (0–NUM_PHASES-1)
    uint32_t phaseStart_ = 0;      // millis() when this phase began

    // -----------------------------------------------------------------------
    // Per-mode update functions
    // -----------------------------------------------------------------------
    void updateStatic();
    void updateBlinking();
    void updateBreathing();
    void updateAnimating();
    void updateCup();
    void updateCounting();
    void updateDrinkReady();
    void updateWorking();
    void updateEStop();

    uint32_t taktMs_ = 400;  // counting takt interval

    // -----------------------------------------------------------------------
    // Idle animation phase handlers (called from updateAnimating)
    // -----------------------------------------------------------------------
    void phaseSpinnerSync        (uint32_t t);   //  0 – 25 s
    void phaseSpinnerOpposed     (uint32_t t);   //  1 – 20 s
    void phasePerimeterChase     (uint32_t t);   //  2 – 20 s
    void phaseBreathingBars      (uint32_t t);   //  3 – 30 s
    void phaseRiseFromBottom     (uint32_t t);   //  4 – 25 s
    void phaseFallFromTop        (uint32_t t);   //  5 – 25 s
    void phaseAlternatingPulse   (uint32_t t);   //  6 – 20 s
    void phaseHeartbeat          (uint32_t t);   //  7 – 30 s
    void phaseSegmentRain        (uint32_t t);   //  8 – 25 s
    void phaseSplitMirror        (uint32_t t);   //  9 – 25 s
    void phaseBouncingBar        (uint32_t t);   // 10 – 25 s
    void phaseExplodeImplode     (uint32_t t);   // 11 – 25 s
    void phasePingPongWave       (uint32_t t);   // 12 – 30 s
    void phaseStrobeAccel        (uint32_t t);   // 13 – 20 s
    void phaseSegmentMorph       (uint32_t t);   // 14 – 30 s
    void phaseCascadeAcross      (uint32_t t);   // 15 – 30 s
    void phaseSnake              (uint32_t t);   // 16 – 35 s
    void phaseSnakeEatsDot       (uint32_t t);   // 17 – 30 s

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    // Gamma-corrected (≈2.0) sine brightness: maps t in [0, period) → [minBr, maxBr]
    static uint8_t sinBright(uint32_t t, uint32_t period,
                             uint8_t minBr = 0, uint8_t maxBr = 255);

    // Gamma correction for linear → perceptual mapping
    static uint8_t gamma(uint8_t v);

    // Set both displays at once
    void show(uint8_t onesMask, uint8_t tensMask,
              uint8_t onesBr  = 255, uint8_t tensBr = 255);
};
