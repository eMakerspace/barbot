#include "SevenSegmentDriver.h"
#include "config.h"
#include <Arduino.h>
#include <soc/gpio_struct.h>   // GPIO.out_w1ts / GPIO.out_w1tc registers

void SevenSegmentDriver::begin() {
    // --- Segment pins ---
    segAllMask_ = 0;
    for (int i = 0; i < 7; i++) {
        pinMode(cfg::SEGMENT[i], OUTPUT);
        digitalWrite(cfg::SEGMENT[i], LOW);
        segMask_[i]  = 1UL << cfg::SEGMENT[i];
        segAllMask_ |= segMask_[i];
    }

    // --- Enable pins ---
    for (int d = 0; d < 2; d++) {
        pinMode(cfg::ENABLE[d], OUTPUT);
        digitalWrite(cfg::ENABLE[d], LOW);
        enMask_[d] = 1UL << cfg::ENABLE[d];
    }

    // --- Precompute per-digit GPIO masks ---
    // Each mask has the GPIO bits set for every segment that is ON for that digit.
    // The refresh loop just does a table lookup + register write, no bit twiddling.
    for (int digit = 0; digit < 10; digit++) {
        digitMask_[digit] = 0;
        for (int s = 0; s < 7; s++) {
            if (cfg::DIGIT_ENCODING[digit] & (1 << s))
                digitMask_[digit] |= segMask_[s];
        }
    }
}

void SevenSegmentDriver::setOnes(uint8_t digit) {
    segmentMask_[0] = cfg::DIGIT_ENCODING[digit % 10];
}

void SevenSegmentDriver::setTens(uint8_t digit) {
    segmentMask_[1] = cfg::DIGIT_ENCODING[digit % 10];
}

void SevenSegmentDriver::setRawMaskOnes(uint8_t mask) {
    segmentMask_[0] = mask;
}

void SevenSegmentDriver::setRawMaskTens(uint8_t mask) {
    segmentMask_[1] = mask;
}

void SevenSegmentDriver::setBrightnessOnes(uint8_t brightness) {
    brightness_[0] = brightness;
}

void SevenSegmentDriver::setBrightnessTens(uint8_t brightness) {
    brightness_[1] = brightness;
}

uint32_t SevenSegmentDriver::segmentMaskToGpio(uint8_t segmentMask) const {
    // Convert a 7-bit segment encoding to a GPIO bitmask
    uint32_t gpioMask = 0;
    for (int s = 0; s < 7; s++) {
        if (segmentMask & (1 << s))
            gpioMask |= segMask_[s];
    }
    return gpioMask;
}

void SevenSegmentDriver::runLoop() {
    while (true) refreshCycle();
}

void SevenSegmentDriver::refreshCycle() {
    // Snapshot shared state once per cycle for consistency
    const uint8_t  mask0     = segmentMask_[0];
    const uint8_t  mask1     = segmentMask_[1];
    const uint32_t gpio0     = segmentMaskToGpio(mask0);
    const uint32_t gpio1     = segmentMaskToGpio(mask1);
    const uint32_t gpios[2]  = { gpio0, gpio1 };

    for (int disp = 0; disp < 2; disp++) {
        // Calculate on-time based on this display's brightness
        const uint8_t  bright = brightness_[disp];
        const uint32_t onUs   = max(1u, cfg::SLOT_US * static_cast<uint32_t>(bright) / 255);
        const uint32_t offUs  = cfg::SLOT_US - onUs;

        // 1. Set segment pins while display is off — prevents ghosting.
        //    out_w1ts sets bits; out_w1tc clears bits (single register write each).
        GPIO.out_w1ts = gpios[disp];                // active segments HIGH
        GPIO.out_w1tc = segAllMask_ & ~gpios[disp]; // inactive segments LOW
        delayMicroseconds(1);                        // VLN2803 turn-on: 1 µs max

        // 2. Enable this display
        GPIO.out_w1ts = enMask_[disp];
        if (onUs > 1) delayMicroseconds(onUs - 1);  // hold for on-time

        // 3. Disable display before clearing segments (anode off first eliminates
        //    the residual current path that causes ghosting)
        GPIO.out_w1tc = enMask_[disp];
        delayMicroseconds(1);                        // L293D turn-off: ~450 ns

        // 4. Blank all segment pins
        GPIO.out_w1tc = segAllMask_;
        if (offUs > 1) delayMicroseconds(offUs - 1); // hold for off-time
    }
}
