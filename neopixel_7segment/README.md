# 7-Segment + LED Strip Controller

**PlatformIO Project!!** open with Platform IO extension on VS Code.

ESP32-based controller for a two-digit 7-segment display and a chain of 87 NeoPixel LEDs split across three physically distinct segments.

## Hardware

| Component | Details |
|---|---|
| MCU | ESP32 DevKit v1 |
| Display | Two-digit 7-segment, multiplexed (common anode) |
| LED data pin | GPIO 13 |
| LED total | 87 WS2812 LEDs |

### LED segment layout

All three segments share one continuous data line in this order:

```
[0 … 42]   BAR   — 43 LEDs, horizontal bar
[43 … 58]  RING  — 16 LEDs, circular ring
[59 … 86]  U     — 28 LEDs, U-shape (left leg → bottom → right leg)
```

## Serial interface

Connect at **115200 baud**. Send commands as plain text lines terminated with `\n`.

### Commands

| Command | Description |
|---|---|
| `<number>` | Display a static number (0–99) |
| `-bl <number>` | Blink a number (0–99) |
| `-br <number>` | Breathing brightness effect on a number (0–99) |
| `-i` | Idle LED animations — **default mode on startup** |
| `-c` | Falling cup animation on the display |

### Example session

```
42          → display shows 42 (static)
-bl 7       → display blinks 7
-br 55      → display breathes 55
-i          → all three LED segments start idle animations
-c          → falling cup animation on 7-segment display
```

## Idle animations (`-i`)

All three segments animate **independently and simultaneously**. Each segment cycles through its own pool of effects, picking the next one at random when the current phase ends (20 s per phase by default, configured in `config.h`).

### BAR (LEDs 0–42)

| Effect | Description |
|---|---|
| Rainbow wave | Smooth hue gradient flows along the bar |
| Comet | Bright head races along the strip with a decaying white tail |
| Twinkle | Random pixels flash to random colours and fade |
| Breathing | Whole bar slowly breathes deep blue/cyan |
| Theater chase | Every-3rd-pixel chase with slowly rotating hue |
| Colour wipe | Fills pixel-by-pixel in red → green → blue, then clears |
| Scanner | KITT-style single pixel bounces back and forth |

### RING (LEDs 43–58)

| Effect | Description |
|---|---|
| Spin | A short arc of light rotates around the ring (1.5 s/rev) |
| Pulse | Whole ring breathes with a slowly cycling hue (2.5 s cycle) |
| Cylon | Single bright dot chases continuously around the ring with tail |

### U-SHAPE (LEDs 59–86)

| Effect | Description |
|---|---|
| Fill | Fills symmetrically from both tops toward the bottom, then clears |
| Bounce | Comet travels down the left leg, around the bottom, and up the right leg, then reverses |
| Meteor | Two meteors descend both legs simultaneously and meet at the base |

## Project structure

```
src/
  config.h               — all pin assignments, LED layout constants, timing
  main.cpp               — setup, loop, command dispatch
  SerialParser.{h,cpp}   — serial line reader and command parser
  DisplayController.{h,cpp} — 7-segment animation state machine
  SevenSegmentDriver.{h,cpp} — low-level multiplexed display driver (core 0)
  LedStripController.{h,cpp} — WS2812 animation engine (core 1)
  Fader.{h,cpp}          — brightness fader helper
```

## Building

```bash
pio run              # compile
pio run -t upload    # compile and flash
pio device monitor   # open serial monitor at 115200 baud
```

## Adding a new LED animation

1. Add `LED_DUR_<NAME>` in `config.h`
2. Append the duration to the appropriate `*_PHASES[]` array in `LedStripController.cpp`
3. Add a `case` in the matching `update*()` dispatcher
4. Implement `anim<Name>(uint32_t t)` — write only into your segment's index range
