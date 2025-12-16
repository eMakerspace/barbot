use embedded_io_async::Read;
use esp_hal::usb_serial_jtag::UsbSerialJtagRx;
use esp_println::print;
use num_traits::float::FloatCore;
use num_traits::ToPrimitive;

use crate::cmd::{Cmd, CmdChannel, StepperCmd, StopCmd, StopCmdPub};

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
        log::error!("invalid command: {gcmd}");
    };

    match gcmd.mnemonic() {
        gcode::Mnemonic::General => match gcmd.major_number() {
            // `G0 X{position}` Move stepper motor to step {position}.
            0 => {
                if let Some(loc) = gcmd.value_for('X') {
                    let Some(loc) = (loc * 8.0).round().to_i32() else {
                        invalid_cmd();
                        return;
                    };

                    cmd_chan.send(Cmd::Stepper(StepperCmd::GoTo(loc))).await;
                }
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
                stop_pub.publish_immediate(StopCmd { force: true });
                log::info!("Stopping immediately from command");
            }
            // - `M0` stops slowly.
            0 => {
                stop_pub.publish(StopCmd { force: false }).await;
                log::info!("Stopping early from command");
            }
            // `M10` Toggle local echo.
            10 => {
                *should_echo = !*should_echo;
            }
            _ => invalid_cmd(),
        },
        _ => invalid_cmd(),
    }
}
