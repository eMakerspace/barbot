use embassy_time::Timer;
use esp_hal::gpio::Output;
use futures::{FutureExt, select_biased};

use crate::cmd::{ServoCmdSignal, StopCmdSub};

/// Convert angle (0–180°) to PWM pulse width in microseconds.
/// Power HD LW-30MG: center pulse = 1520µs, ±1000µs range.
/// 520µs = 0°, 1520µs = 90°, 2520µs = 180°.
fn angle_to_pulse_us(angle: u8) -> u64 {
    520 + angle.min(180) as u64 * 2000 / 180
}

#[embassy_executor::task]
pub async fn servo_task(
    cmd_sig: &'static ServoCmdSignal,
    mut stop_sub: StopCmdSub,
    mut pin: Output<'static>,
) {
    // Home the servo to 180° on startup before accepting any commands.
    // Hold for ~500 ms (25 × 20 ms cycles) so it physically reaches the position.
    let mut angle: u8 = 180;
    for _ in 0..25u8 {
        let pulse_us = angle_to_pulse_us(angle);
        pin.set_high();
        Timer::after_micros(pulse_us).await;
        pin.set_low();
        Timer::after_micros(20_000 - pulse_us).await;
    }

    loop {
        let pulse_us = angle_to_pulse_us(angle);

        // High pulse — bail early on stop
        pin.set_high();
        select_biased! {
            _ = Timer::after_micros(pulse_us).fuse() => {},
            _ = stop_sub.next_message_pure().fuse() => {
                pin.set_low();
                continue;
            },
        }
        pin.set_low();

        // Rest of 20ms period — accept new angle command or stop
        select_biased! {
            _ = Timer::after_micros(20_000 - pulse_us).fuse() => {},
            _ = stop_sub.next_message_pure().fuse() => {},
            cmd = cmd_sig.receive().fuse() => {
                angle = cmd.into_inner().angle;
            },
        }
    }
}
