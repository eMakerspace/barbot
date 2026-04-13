#pragma once
#include <cstdint>

// SevenSegmentDriver: multiplexed two-digit 7-segment display driver.
//
// Designed for two common-anode displays sharing the same segment pins.
// One display is enabled at a time; they alternate fast enough to appear
// simultaneously lit (persistence of vision at 250 Hz).
//
// Supports:
//   • setOnes/setTens: display a specific digit (0–9)
//   • setRawMask: display a custom 7-bit segment pattern on a specific display
//   • Per-display brightness control
//
// Efficiency techniques used:
//   • Precomputed GPIO bitmasks: a digit switch costs one 32-bit register
//     write instead of 7 individual digitalWrite() calls (~20× faster).
//   • Direct GPIO register access (out_w1ts / out_w1tc) bypasses the Arduino
//     abstraction and writes directly to the ESP32 set/clear registers.
//   • Brightness via on-time modulation: no PWM peripheral used, so there is
//     zero PWM-cycle latency. Blanking takes effect in nanoseconds.
//   • Volatile 8-bit shared variables: single-byte reads/writes are atomic on
//     the Xtensa LX6 — no mutex needed between the two cores.
//   • [[noreturn]] on runLoop() lets the compiler omit the return epilogue.
class SevenSegmentDriver {
public:
    // Initialise GPIO and precompute all bitmasks.
    void begin();

    // Setters — safe to call from any core (atomic 8-bit writes).
    // For digit display: 0–9 → display that digit
    // For raw mask: directly set which segments are on (bits 0–6 = a–g)
    void setOnes(uint8_t digit);
    void setTens(uint8_t digit);
    void setRawMaskOnes(uint8_t segmentMask);
    void setRawMaskTens(uint8_t segmentMask);

    // Per-display brightness (0–255)
    void setBrightnessOnes(uint8_t brightness);
    void setBrightnessTens(uint8_t brightness);

    // Blocks forever — must be called from a dedicated FreeRTOS task.
    [[noreturn]] void runLoop();

private:
    // One full multiplex cycle: show ones display, then tens display.
    void refreshCycle();

    // Helper: convert a 7-bit segment encoding to a GPIO bitmask
    uint32_t segmentMaskToGpio(uint8_t segmentMask) const;

    // GPIO bitmasks (precomputed once in begin())
    uint32_t segMask_[7]    = {};   // one mask per segment pin
    uint32_t segAllMask_    = 0;    // OR of all segment masks
    uint32_t enMask_[2]     = {};   // one mask per enable pin
    uint32_t digitMask_[10] = {};   // GPIO mask for each digit 0–9

    // Shared state between cores (written by core 1, read by core 0).
    // Declared volatile to prevent the compiler caching them in registers.
    // Each display has its own 7-bit segment mask (0–9 = digit, 0b0000000–0b1111111 = custom)
    volatile uint8_t segmentMask_[2] = { 0b0111111, 0b0111111 };  // {ones, tens}
    volatile uint8_t brightness_[2]  = { 255, 255 };              // {ones, tens}
};
