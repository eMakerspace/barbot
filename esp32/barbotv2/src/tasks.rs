use embassy_time::Timer;
use esp_hal::gpio::{Input, Level};
use futures::{FutureExt, select_biased};


pub mod servo;
pub mod route_cmd;
pub mod serial;
pub mod stepper;

#[embassy_executor::task]
pub async fn cup_presence_monitor(
    mut cup_sensor: Input<'static>,
) {
    let mut last_state = cup_sensor.level();
    // Emit initial state so the Pi can sync _cup_present on boot
    if last_state == Level::Low {
        log::info!("[cup] PRESENT\r");
    } else {
        log::info!("[cup] ABSENT\r");
    }
    log::info!("Cup presence sensor initialized\r");

    loop {
        select_biased! {
            _ = cup_sensor.wait_for_any_edge().fuse() => (),
            _ = Timer::after_millis(100).fuse() => (),
        }

        let current_state = cup_sensor.level();

        // Report state changes (LOW = cup present, HIGH = cup absent)
        if current_state != last_state {
            if current_state == Level::Low {
                log::info!("[cup] PRESENT\r");
            } else {
                log::info!("[cup] ABSENT\r");
            }
            last_state = current_state;
        }
    }
}
