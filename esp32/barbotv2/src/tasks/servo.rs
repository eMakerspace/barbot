use embedded_hal::pwm::SetDutyCycle;
use esp_hal::ledc::{
    self,
    channel::{self, ChannelIFace},
    timer::{self, TimerIFace},
    LowSpeed, LSGlobalClkSource,
};
use esp_hal::time::Rate;
use futures::{FutureExt, select_biased};
use static_cell::StaticCell;

use crate::cmd::{ServoCmdSignal, StopCmdSub};

// Static storage so Ledc and Timer outlive the task future (no self-referential borrow).
static LEDC_INST:  StaticCell<ledc::Ledc<'static>>              = StaticCell::new();
static LEDC_TIMER: StaticCell<ledc::timer::Timer<'static, LowSpeed>> = StaticCell::new();

/// Convert servo angle (0–180°) to a 14-bit LEDC duty count.
///
/// Power HD LW-30MG: 0° → 520µs, 90° → 1520µs, 180° → 2520µs, period = 20 000µs.
/// 14-bit resolution = 16 384 counts → duty = pulse_us * 16 384 / 20 000.
fn angle_to_duty(angle: u8) -> u16 {
    let pulse_us = 520u32 + angle.min(180) as u32 * 2000 / 180;
    (pulse_us * 16_384 / 20_000) as u16
}

#[embassy_executor::task]
pub async fn servo_task(
    cmd_sig:         &'static ServoCmdSignal,
    mut stop_sub:    StopCmdSub,
    ledc_peripheral: esp_hal::peripherals::LEDC<'static>,
    servo_pin:       esp_hal::peripherals::GPIO9<'static>,
) {
    // ── One-time LEDC hardware setup ─────────────────────────────────────────
    // Store Ledc and Timer in statics so Channel can hold a 'static reference
    // to the timer without creating a self-referential future.
    let ledc = LEDC_INST.init(ledc::Ledc::new(ledc_peripheral));
    ledc.set_global_slow_clock(LSGlobalClkSource::APBClk);

    let lstimer = LEDC_TIMER.init(ledc.timer::<LowSpeed>(timer::Number::Timer0));
    lstimer
        .configure(timer::config::Config {
            duty:         timer::config::Duty::Duty14Bit,
            clock_source: ledc::timer::LSClockSource::APBClk,
            frequency:    Rate::from_hz(50), // 50 Hz = 20 ms period
        })
        .expect("LEDC timer configure failed");

    let mut channel = ledc.channel(channel::Number::Channel0, servo_pin);
    channel
        .configure(channel::config::Config {
            timer:      lstimer,
            duty_pct:   0,
            drive_mode: esp_hal::gpio::DriveMode::PushPull,
        })
        .expect("LEDC channel configure failed");

    // Home to 180° (closed) and hold for 500 ms
    channel.set_duty_cycle(angle_to_duty(180)).ok();
    embassy_time::Timer::after_millis(500).await;

    log::info!("Servo ready (hardware LEDC PWM at 50 Hz)\r");

    // ── Command loop ─────────────────────────────────────────────────────────
    loop {
        select_biased! {
            _ = stop_sub.next_message_pure().fuse() => {
                // On stop return to safe closed position
                channel.set_duty_cycle(angle_to_duty(180)).ok();
                crate::CURRENT_SERVO_ANGLE.store(180, portable_atomic::Ordering::Relaxed);
            },
            cmd = cmd_sig.receive().fuse() => {
                let angle = cmd.into_inner().angle;
                channel.set_duty_cycle(angle_to_duty(angle)).ok();
                crate::CURRENT_SERVO_ANGLE.store(angle as i32, portable_atomic::Ordering::Relaxed);
            },
        }
    }
}
