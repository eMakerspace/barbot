# ESP32c3 firmware of barbot HAT

Uses Rust and [no-std esp-rs](https://docs.esp-rs.org/book/overview/using-the-core-library.html) crates for the firmware.
It is possible to simulate the esp32 firmware with
[Wokwi](https://docs.wokwi.com/?utm_source=wokwi), for this you must install the Wokwi
extension in vscode.

We use the [Seeed Studio XIAO esp32c3
board](https://www.seeedstudio.com/Seeed-XIAO-ESP32C3-p-5431.html).

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

- [X] Project setup, receving commands, wokwi
- [ ] Control stepper motor
- [ ] Control pumps
- [ ] Stepper motor homing (with end-switch on `GPIO10`)
- [ ] G-code parsing, commands