#pragma once
#include <cstdint>
#include <FastLED.h>
#include "config.h"

// Per-segment animation state — tracks current phase and when it started.
struct SegState {
    int      phase      = 0;
    uint32_t phaseStart = 0;
};

// LedStripController manages all WS2812 LED-strip animations.
//
// Design mirrors DisplayController:
//   - begin()   called once in setup()
//   - tick()    called every loop() — internally rate-limited to cfg::LED_FPS
//   - All animation logic is millis()-based and fully non-blocking
//   - Adding a new idle animation = add a constexpr duration + one function
//
// Core-safety: FastLED.show() is called exclusively from core 1 (loop).
// The 7-segment display task runs on core 0 and never touches the strip.
class LedStripController {
public:
    // Initialise FastLED — call once from setup() after Serial.begin().
    void begin();

    // Advance animation state and push pixels if a new frame is due.
    // Safe to call every loop() iteration — throttled internally.
    void tick();

    // -----------------------------------------------------------------------
    // Mode setters  (add more here as needed)
    // -----------------------------------------------------------------------
    void setIdle();
    void setOff();
    void setStrobe();
    void setCup();
    void setMoving();
    void setPouring();
    void setMixing();
    void setDone();
    void setDrinkReady();
    void setOverdueStrobe();
    void setSolid(const CRGB &color);
    void setFlash(const CRGB &color, uint16_t onMs = 100, uint16_t offMs = 100);
    void setPulse(const CRGB &color, uint16_t periodMs = 1200);

private:
    enum class Mode : uint8_t { Idle, Off, Strobe, Cup, Moving, Pouring, Mixing, Done, DrinkReady, OverdueStrobe, Solid, Flash, Pulse };

    CRGB     leds_[cfg::LED_COUNT];
    Mode     mode_      = Mode::Idle;
    SegState bar_;
    SegState ring_;
    SegState u_;
    uint32_t lastFrame_ = 0;
    uint32_t animStart_ = 0;   // time when current animation mode started
    CRGB     effectColor_ = CRGB::Black;
    uint16_t flashOnMs_ = 100;
    uint16_t flashOffMs_ = 100;
    uint16_t pulsePeriodMs_ = 1200;

    // -----------------------------------------------------------------------
    // Idle animation phases — BAR (indices 0–6)
    // -----------------------------------------------------------------------
    void animRainbowWave  (uint32_t t);   // 0 – smooth hue rotation along bar
    void animComet        (uint32_t t);   // 1 – bright head with fading tail
    void animTwinkle      (uint32_t t);   // 2 – random pixels sparkle
    void animBreathing    (uint32_t t);   // 3 – whole bar breathes one colour
    void animTheaterChase (uint32_t t);   // 4 – every-3rd-pixel chase
    void animColorWipe    (uint32_t t);   // 5 – fill then clear colour by colour
    void animScanner      (uint32_t t);   // 6 – bouncing single bright pixel

    // -----------------------------------------------------------------------
    // Idle animation phases — RING (indices 7–9)
    // -----------------------------------------------------------------------
    void animRingSpin     (uint32_t t);   // 7 – single-colour arc rotates around ring
    void animRingPulse    (uint32_t t);   // 8 – whole ring breathes with hue cycle
    void animRingCylon    (uint32_t t);   // 9 – KITT dot sweeps around ring

    // -----------------------------------------------------------------------
    // Idle animation phases — U-SHAPE (indices 10–12)
    // -----------------------------------------------------------------------
    void animUFill        (uint32_t t);   // 10 – fills from both ends toward bottom
    void animUBounce      (uint32_t t);   // 11 – comet bounces along U path
    void animUMeteor      (uint32_t t);   // 12 – meteors rain down both legs

    // -----------------------------------------------------------------------
    // Activity animation updates
    // -----------------------------------------------------------------------
    void updateCup();
    void updateMoving();
    void updatePouring();
    void updateMixing();
    void updateDone();
    void updateDrinkReady();
    void updateOverdueStrobe();

    // -----------------------------------------------------------------------
    // Internal helpers
    // -----------------------------------------------------------------------
    void updateIdle();
    void show();   // calls FastLED.show() and records lastFrame_

    // Sine brightness: maps t in [0, period) → [lo, hi] with gamma correction
    static uint8_t sinBright(uint32_t t, uint32_t period,
                             uint8_t lo = 0, uint8_t hi = 255);
    static uint8_t gamma8(uint8_t v);
};
