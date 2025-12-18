use embassy_time::Timer;
use esp_hal::gpio::{Input, Level};
use futures::{FutureExt, select_biased};

use crate::cmd::{StopCmd, StopCmdImmediatePub};

pub mod route_cmd;
pub mod serial;
pub mod stepper;
pub mod pump;
pub mod lift_motor;

#[embassy_executor::task]
pub async fn emergency_stop_monitor(
    mut emergency_stop: Input<'static>,
    stop_pub: StopCmdImmediatePub,
) {
    let mut stop_active = false;
    loop {
        select_biased! {
            _ = emergency_stop.wait_for_any_edge().fuse() => (),
            _ = Timer::after_millis(10).fuse() => (),
        }

        let level = emergency_stop.level();

        match level {
            Level::Low if !stop_active => {
                stop_pub.publish_immediate(StopCmd::Immediate);
                stop_active = true;
                log::warn!("Emergency stop!\r");
            }
            Level::High if stop_active => {
                if let Ok(_) = stop_pub.try_publish(StopCmd::Continue) {
                    stop_active = false;
                    log::info!("Emergency stop cleared\r");
                }
            }
            _ => (),
        }
    }
}
