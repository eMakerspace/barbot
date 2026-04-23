# ESP32 Pump + Scale Firmware

PlatformIO firmware for an **ESP32 DevKit v1** that controls mixer pumps and an HX711 load cell.

## Build

```bash
pio run
```

## Default wiring

- HX711 DOUT: `GPIO34`
- HX711 SCK: `GPIO27`
- Pump outputs: `GPIO16`, `GPIO17`, `GPIO18`, `GPIO19`

If your wiring is different, update constants in `src/main.cpp` (`namespace cfg`).

## Serial commands (115200 baud)

- `G2 I{pump} D{ms}`: run pump non-blocking
- `G2.1 I{pump} D{ms}`: run pump and wait
- `G3`: read weight in grams
- `G3.1`: tare scale
- `G3.2 W{grams}`: calibrate with known weight
- `G3.3 F{countsPerGram}`: set calibration factor directly
- `G3.4 N{samples}`: debug samples (raw + grams)
- `G4 I{pump} W{grams}`: pump until target grams are dispensed
- `M0` or `M0.1`: emergency stop pumps
- `M1`: clear stop latch

