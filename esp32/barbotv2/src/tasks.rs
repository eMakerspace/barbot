use embassy_time::Timer;
use esp_hal::gpio::{Input, Level};
use futures::{FutureExt, select_biased};

use crate::cmd::{StopCmd, StopCmdImmediatePub};

pub mod servo;
pub mod pump;
pub mod route_cmd;
pub mod scale;
pub mod serial;
pub mod stepper;

#[embassy_executor::task]
pub async fn emergency_stop_monitor(
    mut emergency_stop: Input<'static>,
    stop_pub: StopCmdImmediatePub,
) {
    let mut stop_active = false;
    let mut stop_clearing = false;
    loop {
        select_biased! {
            _ = emergency_stop.wait_for_any_edge().fuse() => (),
            _ = Timer::after_millis(if stop_clearing { 10 } else { 1000 }).fuse() => (),
        }

        let level = emergency_stop.level();

        match level {
            Level::Low if !stop_active || stop_clearing => {
                stop_pub.publish_immediate(StopCmd::Immediate);
                stop_active = true;
                stop_clearing = false;
                log::warn!("Emergency stop!\r");
            }
            Level::High if stop_active => {
                if let Ok(_) = stop_pub.try_publish(StopCmd::Continue) {
                    stop_active = false;
                    stop_clearing = false;
                    log::info!("Emergency stop cleared\r");
                } else {
                    stop_clearing = true;
                }
            }
            _ => (),
        }
    }
}
