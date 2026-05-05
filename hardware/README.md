# Modular Hardware

- Nothing is fixed; it will change anyway...
- Make a solid base for creative hacking
- Everything based on ribbon cable
- No free flying hardware and sketchy cables
- Every cable is keyed, no wrong polarities.

| &nbsp;                                    | &nbsp;                                          |
| ----------------------------------------- | ----------------------------------------------- |
| ![PCB Top](img/proto_board_top.png)       | ![PCB Bottom](img/proto_board_bottom.png)       |
| ![PCB 3D Top](img/proto_board_3d_top.png) | ![PCB 3D Bottom](img/proto_board_3d_bottom.png) |

## Underling Architecture

- Different boards e.g.
    - IO
    - DC Motors with H-Bridge
    - Stepper motors
    - Proto board
    - Raspberry Pi
    - Main ESP

![architecture.svg](architecture.svg)

## BarBot HAT

First try with a fixed version, failed at the first adaption... ;-)

- Raspberry Pi HAT
- SEED Studio ESP32-C3
- One stepper motor driver
- Five 12V outputs, with a Tiny 1.5 A MOSFET Gate Driver
- Neopixel output

| &nbsp;                              | &nbsp;                                    |
| ----------------------------------- | ----------------------------------------- |
| ![PCB Top](img/board_top.png)       | ![PCB Bottom](img/board_bottom.png)       |
| ![PCB 3D Top](img/board_3d_top.png) | ![PCB 3D Bottom](img/board_3d_bottom.png) |
