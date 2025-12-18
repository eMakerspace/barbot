use embassy_time::Timer;
use esp_hal::gpio::{Level, Output};
use futures::{FutureExt, select_biased};

use crate::cmd::{LiftMotorCmdSignal, StopCmdSub};
use crate::utils::invert_level;

const ACTIVE_LEVEL: Level = Level::High;
pub const INACTIVE_LEVEL: Level = invert_level(ACTIVE_LEVEL);

pub const PUMP_COUNT: usize = 4;
pub type PumpGpios = [Output<'static>; PUMP_COUNT];

#[embassy_executor::task]
pub async fn lift_motor(
    cmd_sig: &'static LiftMotorCmdSignal,
    mut stop_cmd_sub: StopCmdSub,
    mut gpio_up: Output<'static>,
    mut gpio_down: Output<'static>,
) {
    loop {
        let cmd = select_biased! {
            _ = stop_cmd_sub.next_message_pure().fuse() => {
                // Stop any movement.
                gpio_up.set_level(INACTIVE_LEVEL);
                gpio_down.set_level(INACTIVE_LEVEL);
                continue;
            },
            cmd = cmd_sig.receive().fuse() => cmd,
        };

        if cmd.direction_up {
            gpio_down.set_level(INACTIVE_LEVEL);
            gpio_up.set_level(ACTIVE_LEVEL);
        } else {
            gpio_up.set_level(INACTIVE_LEVEL);
            gpio_down.set_level(ACTIVE_LEVEL);
        }

        select_biased! {
            _ = stop_cmd_sub.next_message_pure().fuse() => (),
            _ = Timer::after_millis(cmd.duration_ms as u64).fuse() => ()
        };
        gpio_up.set_level(INACTIVE_LEVEL);
        gpio_down.set_level(INACTIVE_LEVEL);
    }
}
