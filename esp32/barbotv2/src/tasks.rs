use esp_hal::gpio::Input;

use crate::cmd::{StopCmd, StopCmdImmediatePub};

pub mod route_cmd;
pub mod serial;
pub mod stepper;

#[embassy_executor::task]
pub async fn emergency_stop_monitor(
    mut emergency_stop: Input<'static>,
    stop_pub: StopCmdImmediatePub,
) {
    loop {
        emergency_stop.wait_for_falling_edge().await;
        stop_pub.publish_immediate(StopCmd { force: true });
        log::warn!("Emergency stop!\r");
    }
}
