use embassy_time::{Duration, Instant, Timer};
use esp_hal::gpio::{Level, Output};
use futures::{FutureExt, select_biased};

use crate::cmd::{PumpCmdSignal, StopCmd, StopCmdSub};
use crate::utils::invert_level;

const ACTIVE_LEVEL: Level = Level::Low;
pub const INACTIVE_LEVEL: Level = invert_level(ACTIVE_LEVEL);

pub const PUMP_COUNT: usize = 4;
pub type PumpGpios = [Output<'static>; PUMP_COUNT];

#[embassy_executor::task]
pub async fn pump_task(
    cmd_sig: &'static PumpCmdSignal,
    mut stop_cmd_sub: StopCmdSub,
    mut gpios: PumpGpios,
) {
    let mut expirations = [None::<Instant>; PUMP_COUNT];
    let mut to_block = None::<(_, usize, Instant)>;

    loop {
        let next_instant = {
            let maybe_blocking = if let Some((_, idx, inst)) = &to_block {
                Some((*idx, *inst))
            } else {
                None
            };

            expirations
                .iter()
                .enumerate()
                .filter_map(|(i, &inst)| Some((i, inst?)))
                .chain(maybe_blocking)
                .min_by_key(|(_, inst)| *inst)
        };

        let Some((idx, inst)) = next_instant else {
            let cmd = select_biased! {
                _ = stop_cmd_sub.next_message_pure().fuse() => {
                    continue;
                },
                // Note: So that we dont wait forever here, `to_block` must be None.
                // This is gauranteed since if `to_block` is Some, there is at least one
                // active pump with a scheduled expiration, and thus `next_instant` would
                // not be None.
                cmd = cmd_sig.receive().fuse() => cmd,
            };

            let idx = cmd.index as usize;

            // duration_ms == 0 means stop this pump immediately.
            if cmd.duration_ms == 0 {
                gpios[idx].set_level(INACTIVE_LEVEL);
                expirations[idx] = None;
                continue;
            }

            let inst = Instant::now() + Duration::from_millis_floor(cmd.duration_ms as u64);

            // Activate the pump.
            gpios[idx].set_level(ACTIVE_LEVEL);

            if cmd.wait {
                // Keeping `cmd` alive causes the command queue to block on us.
                // We need to make sure that `cmd` is dropped when the pump is turned off.
                to_block = Some((cmd, idx, inst))
            } else {
                expirations[idx] = Some(inst);
            }
            continue;
        };

        select_biased! {
            stop_cmd = stop_cmd_sub.next_message_pure().fuse() => {
                match stop_cmd {
                    StopCmd::Immediate | StopCmd::Graceful => {
                        // Turn off all pumps immediately.
                        for gpio in gpios.iter_mut() {
                            gpio.set_level(INACTIVE_LEVEL);
                        }
                        for exp in expirations.iter_mut() {
                            *exp = None;
                        }
                    }
                    StopCmd::Continue => {
                        // Ignore continue if not stopped.
                    }
                }
            },
            _ = Timer::at(inst).fuse() => {
                // Time to turn off pump idx.
                gpios[idx].set_level(INACTIVE_LEVEL);
                expirations[idx] = None;
                if let Some((_, b_idx, _)) = &to_block && *b_idx == idx {
                    to_block = None;
                }
            },
            cmd = cmd_sig.receive().fuse() => {
                let cmd_idx = cmd.index as usize;

                // duration_ms == 0 means stop this pump immediately.
                if cmd.duration_ms == 0 {
                    gpios[cmd_idx].set_level(INACTIVE_LEVEL);
                    expirations[cmd_idx] = None;
                    if let Some((_, b_idx, _)) = &to_block && *b_idx == cmd_idx {
                        to_block = None;
                    }
                } else {
                    let cmd_inst = Instant::now() + Duration::from_millis_floor(cmd.duration_ms as u64);
                    gpios[cmd_idx].set_level(ACTIVE_LEVEL);
                    if to_block.is_some() {
                        log::warn!("Pump command received while blocking\r");
                    }
                    if cmd.wait {
                        to_block = Some((cmd, cmd_idx, cmd_inst))
                    } else {
                        expirations[cmd_idx] = Some(cmd_inst);
                    }
                }
            },
        }
    }
}
