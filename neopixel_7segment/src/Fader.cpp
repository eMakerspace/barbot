#include "Fader.h"
#include <Arduino.h>

bool Fader::tick() {
    const uint32_t elapsed = millis() - stateStart_;

    switch (state_) {
        case State::FadeIn:
            brightness_ = (elapsed >= fadeMs_) ? 255 : gamma(elapsed * 255 / fadeMs_);
            if (elapsed >= fadeMs_) transition(State::Hold);
            break;

        case State::Hold:
            if (elapsed >= holdMs_) transition(State::FadeOut);
            break;

        case State::FadeOut:
            brightness_ = (elapsed >= fadeMs_) ? 0 : gamma((fadeMs_ - elapsed) * 255 / fadeMs_);
            if (elapsed >= fadeMs_) {
                brightness_ = 0;
                transition(State::FadeIn);
                return true;  // signal: advance the counter now
            }
            break;
    }
    return false;
}

uint8_t Fader::gamma(uint32_t v) {
    if (v > 255) v = 255;
    return static_cast<uint8_t>((v * v) / 255);
}

void Fader::transition(State next) {
    state_      = next;
    stateStart_ = millis();
}
