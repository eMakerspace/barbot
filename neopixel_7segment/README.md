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
| `<number>` | Display a static number (0–99) on 7-segment |
| `-bl <number>` | Blink a number (0–99) on 7-segment |
| `-br <number>` | Breathing brightness effect on a number (0–99) on 7-segment |
| `-i` | Idle LED animations on all segments — **default mode on startup** |
| `-c` | Falling cup animation: 7-segment cup + vibrant flashing ring |
| `-mv` | Moving to slot: animates NeoPixel ring & segments, preserves 7-segment display |
| `-pour` | Pouring animation: animates NeoPixel ring & segments, preserves 7-segment display |
| `-mix` | Mixing animation: animates NeoPixel ring & segments, preserves 7-segment display |
| `-done` | Celebratory animation on NeoPixel (rainbow confetti burst + pulse cycle) |
| `-s` | Fast red strobe on all LEDs |
| `-drinkready` | Drink ready: green ring pulse + attention patterns on bar/U |
| `-drinknum <num>` | Drink ready with order number blinking on 7-segment |
| `-overduestrobe` | Overdue alert: full red strobe everything |
| `-e <effect>` | Error/status effect: `IDLE`, `RED_SOLID`, `RED_FLASH_FAST`, `RED_PULSE`, `YELLOW_SOLID`, `ORANGE_FLASH`, `GREEN_SOLID`, `GREEN_FLASH_SLOW`, `GREEN_FLASH_FAST` |

### Example session

```
42          → display shows 42 (static on 7-segment)
-bl 7       → display blinks 7 (7-segment only)
-br 55      → display breathes 55 (7-segment only)
-i          → all three LED segments start idle animations
-c          → falling cup on 7-segment + vibrant flash on ring
-mv         → animate ring/segments, keep previous 7-segment display
-done       → celebrate with rainbow confetti burst on NeoPixel
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
