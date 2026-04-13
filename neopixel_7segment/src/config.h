#pragma once
#include <cstdint>

// All hardware pin assignments and timing constants in one place.
// Only this file needs to change when adapting to different hardware.
namespace cfg {

    // Common-anode enable pins: index 0 = ones (10⁰), index 1 = tens (10¹)
    constexpr uint8_t ENABLE[2]  = { 21, 22 };

    // Segment pins a–g, shared (multiplexed) between both displays
    //   index: 0=a  1=b  2=c  3=d  4=e  5=f  6=g
    //   layout:
    //       aaa
    //      f   b
    //      f   b
    //       ggg
    //      e   c
    //      e   c
    //       ddd
    constexpr uint8_t SEGMENT[7] = { 18, 19, 4, 2, 16, 5, 17 };

    // Multiplexer on-time per display per cycle (µs).
    // 2 000 µs × 2 displays = 4 ms cycle = 250 Hz refresh — flicker-free.
    constexpr uint32_t SLOT_US = 2000;

    // Brightness fade timing
    constexpr uint32_t HOLD_MS = 600;   // hold at full brightness before fading
    constexpr uint32_t FADE_MS = 300;   // duration of each fade-out / fade-in

    // 7-segment digit encoding: bit 0 = segment a … bit 6 = segment g.
    // A set bit means that segment is ON for that digit.
    //
    //  bit index:  6 5 4 3 2 1 0
    //  segment:    g f e d c b a
    constexpr uint8_t DIGIT_ENCODING[10] = {
        0b0111111,  // 0: a b c d e f
        0b0000110,  // 1: b c
        0b1011011,  // 2: a b d e g
        0b1001111,  // 3: a b c d g
        0b1100110,  // 4: b c f g
        0b1101101,  // 5: a c d f g
        0b1111101,  // 6: a c d e f g
        0b0000111,  // 7: a b c
        0b1111111,  // 8: a b c d e f g
        0b1101111,  // 9: a b c d f g
    };

    // -----------------------------------------------------------------------
    // WS2812 LED strip — three physically distinct segments on one data line
    //
    //   [0 … 42]   BAR    – 43 LEDs, horizontal bar
    //   [43 … 58]  RING   – 16 LEDs, circular ring
    //   [59 … 86]  U      – 28 LEDs, U-shape (left leg → bottom → right leg)
    // -----------------------------------------------------------------------
    constexpr uint8_t  LED_PIN   = 13;
    constexpr uint16_t LED_COUNT = 87;   // 43 + 16 + 28

    // Segment start indices and lengths
    constexpr uint16_t BAR_START  =  0;
    constexpr uint16_t BAR_LEN    = 43;
    constexpr uint16_t RING_START = 43;
    constexpr uint16_t RING_LEN   = 16;
    constexpr uint16_t U_START    = 59;
    constexpr uint16_t U_LEN      = 28;

    // Target LED refresh rate (fps). tick() is a no-op if called more often.
    constexpr uint32_t LED_FPS   = 30;
    constexpr uint32_t LED_FRAME_MS = 1000 / LED_FPS;   // ~33 ms

    // Idle animation phase durations (ms)
    // Bar phases
    constexpr uint32_t LED_DUR_RAINBOW_WAVE   = 20000;
    constexpr uint32_t LED_DUR_COMET          = 20000;
    constexpr uint32_t LED_DUR_TWINKLE        = 20000;
    constexpr uint32_t LED_DUR_BREATHING      = 20000;
    constexpr uint32_t LED_DUR_THEATER_CHASE  = 20000;
    constexpr uint32_t LED_DUR_COLOR_WIPE     = 20000;
    constexpr uint32_t LED_DUR_SCANNER        = 20000;
    // Ring phases
    constexpr uint32_t LED_DUR_RING_SPIN      = 20000;
    constexpr uint32_t LED_DUR_RING_PULSE     = 20000;
    constexpr uint32_t LED_DUR_RING_CYLON     = 20000;
    // U-shape phases
    constexpr uint32_t LED_DUR_U_FILL         = 20000;
    constexpr uint32_t LED_DUR_U_BOUNCE       = 20000;
    constexpr uint32_t LED_DUR_U_METEOR       = 20000;

} // namespace cfg
