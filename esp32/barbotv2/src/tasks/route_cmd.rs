use embassy_time::Timer;
use futures::{FutureExt, select_biased};

use crate::cmd::{Cmd, CmdChannel, StepperCmdSignal, StopCmdSub};

pub struct HandleCmd {
    pub cmd_chan: &'static CmdChannel,
    pub stepper_sig: &'static StepperCmdSignal,
    pub pump_sig: &'static crate::cmd::PumpCmdSignal,
    pub stop_sub: StopCmdSub,
    pub servo_sig: &'static crate::cmd::ServoCmdSignal,
}

#[embassy_executor::task]
pub async fn route_cmd(handle_cmd: HandleCmd) {
    let HandleCmd {
        cmd_chan,
        stepper_sig,
        pump_sig,
        mut stop_sub,
        servo_sig,
    } = handle_cmd;

    let mut stopped = false;

    loop {
        if stopped {
            // Wait for a continue command.
            let cmd = stop_sub.next_message_pure().await;
            match cmd {
                crate::cmd::StopCmd::Continue => {
                    stopped = false;
                }
                _ => {
                    continue;
                }
            }
        }

        let cmd = select_biased! {
            stop_cmd = stop_sub.next_message_pure().fuse() => {
                stopped = stop_cmd == crate::cmd::StopCmd::Immediate;
                continue;
            },
            cmd = cmd_chan.receive().fuse() => cmd,
        };

        match cmd {
            Cmd::Pump(pump_cmd) => {
                pump_sig.send(pump_cmd).await;
            }
            Cmd::Stepper(stepper_cmd) => {
                let is_move = !matches!(stepper_cmd, crate::cmd::StepperCmd::Home());

                // SAFETY: Servo MUST be at 180° (safe/closed) before stepper moves.
                let current_servo_angle = crate::CURRENT_SERVO_ANGLE.load(portable_atomic::Ordering::Relaxed);
                if current_servo_angle != 180 {
                    log::warn!("Servo not at safe position ({}°), moving to 180° before stepper\r", current_servo_angle);
                    servo_sig.send(crate::cmd::ServoCmd { angle: 180 }).await;
                    // Wait for servo to physically move to safe position
                    Timer::after_millis(600).await;
                }

                stepper_sig.send(stepper_cmd).await;
                if is_move {
                    log::info!("Move done\r");
                }
            }
            Cmd::Servo(servo_cmd) => {
                // Forbidden zone: middle region between slot 4 and slot 5.
                // Keep a +/- buffer around both slot positions as servo-allowed.
                // Positions are sent by the Pi at boot via G5. Until then, blocking is disabled.
                let slot4 = crate::SLOT4_POS.load(portable_atomic::Ordering::Relaxed);
                let slot5 = crate::SLOT5_POS.load(portable_atomic::Ordering::Relaxed);
                let stepper_pos = crate::STEPPER_X_POS.load(portable_atomic::Ordering::Relaxed);
                let servo_angle = servo_cmd.angle;

                let zones_configured = slot4 != i32::MIN && slot5 != i32::MIN;
                if zones_configured {
                    let zone_min = slot4.min(slot5) + crate::SERVO_ZONE_BUFFER;
                    let zone_max = slot4.max(slot5) - crate::SERVO_ZONE_BUFFER;
                    let in_zone  = zone_min <= zone_max && stepper_pos >= zone_min && stepper_pos <= zone_max;

                    if in_zone && servo_angle < 120 {
                        crate::led::set_error(crate::led::ErrorState::ServoForbiddenZone);
                        log::warn!(
                            "Servo blocked: pos={} in forbidden zone [{},{}], angle={} not safe (need >= 120)\r",
                            stepper_pos, zone_min, zone_max, servo_angle
                        );
                        // Skip the send — servo stays put
                    } else {
                        servo_sig.send(servo_cmd).await;
                        crate::led::clear_error();
                    }
                } else {
                    // Slot positions not yet received from Pi — allow all servo movement
                    servo_sig.send(servo_cmd).await;
                }
            }
            Cmd::Wait(time_ms) => {
                Timer::after_millis(time_ms as u64).await;
            }
        }
    }
}
