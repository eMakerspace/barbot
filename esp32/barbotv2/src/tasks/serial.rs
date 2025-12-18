use embedded_io_async::Read;
use esp_hal::usb_serial_jtag::UsbSerialJtagRx;
use esp_println::print;
use num_traits::ToPrimitive;
use num_traits::float::FloatCore;

use crate::cmd::{Cmd, CmdChannel, PumpCmd, StepperCmd, StopCmd, StopCmdPub};

/// Read from the USB serial and execute the commands
#[embassy_executor::task]
pub async fn serial_reader(
    mut rx: UsbSerialJtagRx<'static, esp_hal::Async>,
    cmd_chan: &'static CmdChannel,
    stop_pub: StopCmdPub,
) {
    let mut buf = [0_u8; 128];

    let mut total_read: usize = 0;
    let mut ignore_next_command = false;
    let mut should_echo = true;
    loop {
        let count_read = match rx.read(&mut buf[total_read..]).await {
            Err(err) => {
                log::error!("{err}");
                continue;
            }
            Ok(val) => val,
        };

        // Echo.
        if should_echo {
            esp_println::Printer::write_bytes(&buf[total_read..total_read + count_read]);
        }

        if let Some((idx, c)) = buf[total_read..total_read + count_read]
            .iter()
            .enumerate()
            .find(|(_i, c)| **c == b'\r' || **c == b'\n')
        {
            if should_echo {
                // Echo the appropriate missing character so that we echo a CRLF or LFCR,
                // for a true linebreak.
                if *c == b'\n' {
                    print!("\r");
                } else if total_read + idx + 1 == total_read + count_read {
                    print!("\n");
                }
            }

            total_read += idx;
            let s = match core::str::from_utf8(&buf[..total_read]) {
                Ok(s) => s,
                Err(_) => {
                    log::warn!("invalid utf-8 received");
                    continue;
                }
            };

            if !ignore_next_command {
                handle_cmd(s.trim(), cmd_chan, &mut should_echo, &stop_pub).await;
            }
            ignore_next_command = false;

            let range_start = total_read + 1;
            let range_end = total_read - idx + count_read;
            buf.copy_within(range_start..range_end, 0);
            total_read = count_read - idx - 1;
        } else {
            total_read += count_read;
        }

        if total_read == buf.len() {
            log::warn!("ignoring command, buffer full");
            total_read = 0;
            ignore_next_command = true;
        }
    }
}

async fn handle_cmd(
    cmd: &str,
    cmd_chan: &'static CmdChannel,
    should_echo: &mut bool,
    stop_pub: &StopCmdPub,
) {
    let gcmd = match gcode::parse(cmd).next() {
        Some(cmd) => cmd,
        None => {
            log::warn!("received cmd '{cmd}' is not valid gcode");
            return;
        }
    };

    let invalid_cmd = || {
        log::error!("invalid command \"{gcmd}\"\r");
    };

    let invalid_cmd_msg = |msg: &str| {
        log::error!("invalid command \"{gcmd}\": {msg}\r");
    };

    match gcmd.mnemonic() {
        gcode::Mnemonic::General => match gcmd.major_number() {
            // `G0.1 X{range_fact}` for moving the stepper motor to `range_fact * end_step`.
            0 if gcmd.minor_number() == 1 => {
                let Some(range_fact) = gcmd.value_for('X') else {
                    invalid_cmd_msg("missing X parameter");
                    return;
                };
                if range_fact < 0.0 || range_fact > 1.0 {
                    invalid_cmd_msg("X parameter out of range");
                    return;
                }
                cmd_chan
                    .send(Cmd::Stepper(StepperCmd::GoToRangeFact(range_fact)))
                    .await;
            }
            // `G0 X{position}` Move stepper motor to step {position}.
            0 => {
                if let Some(loc) = gcmd.value_for('X') {
                    let Some(loc) = (loc * 8.0).round().to_i32() else {
                        invalid_cmd_msg("X parameter out of range");
                        return;
                    };

                    cmd_chan.send(Cmd::Stepper(StepperCmd::GoTo(loc))).await;
                } else {
                    invalid_cmd_msg("missing X parameter");
                    return;
                }
            }
            // `G1.0 Z{time_ms}` to move the lift motor down for `{time_ms}` milliseconds.
            // `G1.1 Z{time_ms}` to move the lift motor up for `{time_ms}` milliseconds.
            1 => {
                let Some(time_ms) = gcmd.value_for('Z') else {
                    invalid_cmd_msg("missing Z<time_ms> parameter");
                    return;
                };
                let Some(time_ms) = time_ms.round().to_u32() else {
                    invalid_cmd_msg("Z parameter out of range");
                    return;
                };

                let is_up = match gcmd.minor_number() {
                    0 => false,
                    1 => true,
                    _ => {
                        invalid_cmd_msg("minor number must be 0 (down) or 1 (up)");
                        return;
                    }
                };

                cmd_chan
                    .send(Cmd::LiftMotor(crate::cmd::LiftMotorCmd {
                        direction_up: is_up,
                        duration_ms: time_ms,
                    }))
                    .await;
            }
            // `G2 N{pump_index} T{time_ms}` Activate pump number {pump_index} for {time_ms} milliseconds.
            // `G2.1 N{pump_index} T{time_ms}` Same as G2, but blocks until the command is complete.
            2 => {
                let Some(pump_index) = gcmd.value_for('I') else {
                    invalid_cmd_msg("missing I<pump_index> parameter");
                    return;
                };
                let Some(time_ms) = gcmd.value_for('D') else {
                    invalid_cmd_msg("missing D<time_ms> parameter");
                    return;
                };

                let Some(pump_index) = pump_index.round().to_u8() else {
                    invalid_cmd_msg("I parameter out of range");
                    return;
                };
                let Some(time_ms) = time_ms.round().to_u32() else {
                    invalid_cmd_msg("D parameter out of range");
                    return;
                };

                cmd_chan
                    .send(Cmd::Pump(PumpCmd {
                        index: pump_index,
                        duration_ms: time_ms,
                        wait: gcmd.minor_number() == 1,
                    }))
                    .await;
            }
            // `G28` start homing.
            28 => {
                cmd_chan.send(Cmd::Stepper(StepperCmd::Home())).await;
            }
            _ => invalid_cmd(),
        },
        gcode::Mnemonic::Miscellaneous => match gcmd.major_number() {
            // Stop the stepper motor.
            // - `M0.1` stops immediately.
            0 if gcmd.minor_number() == 1 => {
                stop_pub.publish_immediate(StopCmd::Immediate);
                log::info!("Stopping immediately from command");
            }
            // - `M0` stops slowly.
            0 => {
                stop_pub.publish(StopCmd::Graceful).await;
                log::info!("Stopping early from command");
            }
            // `M1` recovers from stop.
            1 => {
                stop_pub.publish(StopCmd::Continue).await;
                log::info!("Continuing from stop command");
            }
            // `M10` Toggle local echo.
            10 => {
                *should_echo = !*should_echo;
            }
            // `M`
            _ => invalid_cmd(),
        },
        gcode::Mnemonic::ToolChange => match gcmd.major_number() {
            // `T0 D{time_ms}` Wait for {time_ms} milliseconds.
            0 => {
                let Some(time_ms) = gcmd.value_for('D') else {
                    invalid_cmd_msg("missing D<time_ms> parameter");
                    return;
                };
                let Some(time_ms) = time_ms.round().to_u32() else {
                    invalid_cmd_msg("D parameter out of range");
                    return;
                };
                cmd_chan.send(Cmd::Wait(time_ms)).await;
            }
            _ => invalid_cmd(),
        },
        _ => invalid_cmd(),
    }
}
