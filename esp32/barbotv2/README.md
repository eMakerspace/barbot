# ESP32c3 firmware of barbot HAT

Uses Rust and [no-std esp-rs](https://docs.esp-rs.org/book/overview/using-the-core-library.html) crates for the firmware.
It is possible to simulate the esp32 firmware with
[Wokwi](https://docs.wokwi.com/?utm_source=wokwi), for this you must install the Wokwi
extension in vscode.

We use the [Seeed Studio XIAO esp32c3
board](https://www.seeedstudio.com/Seeed-XIAO-ESP32C3-p-5431.html).

## Pin Assignment

**4 Peristaltic Pumps**

- `PUMP_CTRL0`: `GPIO6`
- `PUMP_CTRL1`: `GPIO5`
- `PUMP_CTRL2`: `GPIO4`
- `PUMP_CTRL3`: `GPIO3`

[**Lift Motor**](https://www.adafruit.com/product/3190)
- `MOTOR_Z_UP`: `GPIO10` (PWM)
- `MOTOR_Z_DOWN`: `GPIO8` (PWM)

**Stepper Motor**
- `MOTOR_X_STEP`: `GPIO20` (PWM)
- `MOTOR_X_DIR`: `GPIO21`

**Misc**
- `End Switch`: `GPIO7` (active low)
- `Neopixel`: `GPIO2` (PWM)
- `Emergency Stop Switch`: `GPIO9` (active low)

## Installation

1. Install the Rust programmin language with [rustup](https://rustup.rs/).
2. Follow [this](https://docs.esp-rs.org/book/installation/riscv.html) to develop for esp32c3.

## Running

### Simulate

You need a Wokwi account, to be able to run simulations within vscode.

1. Activate the Wokwi extension license, press Ctrl-Shift-P and run `Wokwi: Request a New License`.
2. Select the right config, press Ctrl-Shift-P and run `Wokwi: Select Config File`.
3. Simulate with Wokwi, press Ctrl-Shift-P and run `Wokwi: Start Simulator`.

### Flash and Run

Run
```sh
cargo run
```
within a terminal in this directory.
This will run `espflash --monitor --chip <chip> <binary>`,
and should determine the right chip if it is connected with USB.

## Roadmap

- [X] Project setup, receiving commands, wokwi
- [X] Control stepper motor
- [X] Control pumps
- [X] Control lift motor
- [ ] Control LEDs
- [X] Stepper motor homing (with end-switch on `GPIO7`)
    - [X] Reacting to emergency stop (stops the stepper motor immediately).
- [X] G-code parsing, commands
    - [X] `G0 X{position}` for moving the stepper motor to step `{position}`.
    - [X] `G0.1 X{range_fact}` for moving the stepper motor to `range_fact * end_step`
          (where `end_step` is maximum step after homing). This allows specifying
          fractional positions within the homed range (`0` is the start, `1` is the end).
    - [X] `G1.0 Z{time_ms}` to move the lift motor down for `{time_ms}` milliseconds.
    - [X] `G1.1 Z{time_ms}` to move the lift motor up for `{time_ms}` milliseconds.
    - [X] `G2 I{pump} D{time_ms}` activate pump with index `{pump}` for duration `{time_ms}` milliseconds.
    - [X] `G2.1 I{pump} D{time_ms}` activate pump with index `{pump}` for `{time_ms}` milliseconds and wait
          for deactivation.
    - [X] `G28` to start homing.
    - [X] `T0 D{time_ms}` to wait for `{time_ms}` milliseconds.
    - [X] `M0` to stop the current command gracefully (and continue).
    - [X] `M0.1` to stop the current command immediately (emergency stop) and remain stopped until a `M1` command.
    - [X] `M1` recorvers from emergency stop.
    - [X] `M10` to toggle serial echo.