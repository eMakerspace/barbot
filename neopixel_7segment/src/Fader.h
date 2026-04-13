#pragma once
#include <cstdint>

// Fader: gamma-corrected brightness state machine.
//
// Cycles through: FadeIn → Hold → FadeOut → FadeIn → …
//
// Call tick() on every loop iteration.
// It updates brightness() and returns true once per FadeOut completion,
// signalling the caller to advance to the next value.
class Fader {
public:
    constexpr Fader(uint32_t holdMs, uint32_t fadeMs)
        : holdMs_(holdMs), fadeMs_(fadeMs) {}

    // Returns current brightness (0–255)
    [[nodiscard]] uint8_t brightness() const { return brightness_; }

    // Drives the state machine. Returns true when the fade-out completes
    // (i.e., the display just went dark — safe moment to change the value).
    bool tick();

private:
    enum class State : uint8_t { FadeIn, Hold, FadeOut };

    // Gamma ≈ 2.0: maps a linear 0–255 ramp to a perceptually even one.
    // LEDs are not linear; a raw linear ramp looks abrupt at the bright end.
    static uint8_t gamma(uint32_t v);

    void transition(State next);

    const uint32_t holdMs_;
    const uint32_t fadeMs_;

    State    state_      = State::FadeIn;
    uint32_t stateStart_ = 0;
    uint8_t  brightness_ = 0;
};
