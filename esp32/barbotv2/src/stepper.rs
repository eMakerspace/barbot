use embassy_sync::blocking_mutex::raw::CriticalSectionRawMutex;
use embassy_sync::signal::Signal;
use esp_hal::gpio::{Level, Output, OutputConfig, OutputPin};
use esp_hal::peripheral::Peripheral;
use esp_hal::rmt::PulseCode;
use esp_hal::time::Rate;
use num_traits::float::FloatCore;
use stepgen::Stepgen;

use crate::rmt::IterRmt;

pub type StopSignal = Signal<CriticalSectionRawMutex, ()>;

/// Stepper motor driver with absolute positioning and acceleration/deacceleration curve.
pub struct Stepper {
    rmt: IterRmt,
    dir_pin: Output<'static>,
    tick_rate: Rate,
    acceleration: u32,
    max_speed: u32,
    stop_signal: Option<&'static StopSignal>,
    dir_high_positive: bool,
    curr_pos: i32,
}

impl Stepper {
    const N_DECIMALS: u32 = 8;
    const DECIMALS_FACT: f32 = (1 << Self::N_DECIMALS) as f32;

    /// Create a new stepper motor driver.
    /// 
    /// - `rmt`: The RMT peripheral instance to send the waveform with.
    /// - `tick_rate`: Tick rate used as the base time unit, must be the same as the one
    ///   configured for the `rmt` instance (including divider).
    /// - `dir_pin`: The pin to control the turning idrection.
    /// - `dir_high_positive`: When `true`, the motor turns in the positive direction when
    ///   [`Level::High`] is output on the dir pin, otherwise if `false` it turns in the
    ///   negative direction.
    pub fn new(
        rmt: IterRmt,
        tick_rate: Rate,
        dir_pin: impl Peripheral<P = impl OutputPin> + 'static,
        dir_high_positive: bool,
    ) -> Self {
        Self {
            rmt,
            dir_pin: Output::new(
                dir_pin,
                Level::Low,
                OutputConfig::default().with_drive_mode(esp_hal::gpio::DriveMode::PushPull),
            ),
            tick_rate,
            acceleration: 0,
            max_speed: 0,
            stop_signal: None,
            dir_high_positive,
            curr_pos: 0,
        }
    }

    /// Set the current absolute motor step position.
    pub fn set_curr_pos(&mut self, curr_step: i32) {
        self.curr_pos = curr_step;
    }

    /// Set the acceleration in steps per second^2.
    pub fn set_acceleration(&mut self, steps_per_second_sq: f32) {
        self.acceleration = (steps_per_second_sq * Self::DECIMALS_FACT).round() as u32;
    }

    /// Set the stop signal, which when sent, will stop the motor.
    ///
    /// TODO: How fast should the motor deaccelerate? Currently, it does so with the
    /// configured acceleration.
    pub fn set_stop_signal(&mut self, stop_signal: &'static StopSignal) {
        self.stop_signal = Some(stop_signal);
    }

    /// Set the maximum speed in steps per second.
    pub fn set_max_speed(&mut self, steps_per_second: f32) {
        self.max_speed = (steps_per_second * Self::DECIMALS_FACT).round() as u32;
    }

    /// Run to the given `pos` in steps.
    pub async fn run_to_pos(&mut self, pos: i32) -> stepgen::Result {
        let steps = self.curr_pos.abs_diff(pos);
        let dir = if (pos > self.curr_pos) == self.dir_high_positive {
            Level::High
        } else {
            Level::Low
        };

        match self.run_steps(steps, dir).await {
            Ok(()) => {
                self.curr_pos = pos;
                Ok(())
            }
            val => val,
        }
    }

    /// Run the given amount of `steps` into the given `direction`
    /// while smoothly accelerating and deaccelerating.
    pub async fn run_steps(&mut self, steps: u32, dir_level: Level) -> stepgen::Result {
        self.dir_pin.set_level(dir_level);

        let mut gen = Stepgen::new(self.tick_rate.as_hz());
        gen.set_acceleration(self.acceleration)?;
        gen.set_target_speed(self.max_speed)?;
        gen.set_target_step(steps)?;
        let signal = self.stop_signal.clone();

        let mut next_delay = None::<u32>;
        self.rmt
            .transmit(core::iter::from_fn(move || {
                match &signal {
                    Some(sig) if sig.try_take().is_some() => {
                        gen.set_target_step(0).ok();
                        next_delay.take();
                    }
                    _ => (),
                }

                if let Some(delay) = next_delay.take() {
                    const MAX_PERIOD: u32 = (1 << 15) - 1;
                    let delay_prime = delay - 1;
                    let first_period = delay_prime.min(MAX_PERIOD);
                    let second_period = (delay - first_period).min(MAX_PERIOD);

                    if first_period + second_period != delay {
                        log::warn!("stepper motor too slow: delay {delay} > {MAX_PERIOD}");
                    }

                    return Some(PulseCode::new(
                        Level::Low,
                        first_period as u16,
                        Level::Low,
                        second_period as u16,
                    ));
                }

                let mut delay = gen.next()? >> 7;
                // println!("delay: {delay}");
                let pulse_delay = delay.min(3).max(2) as u16;
                delay = delay.saturating_sub(pulse_delay as u32);
                if delay > 0 {
                    next_delay = Some(delay);
                }

                Some(PulseCode::new(Level::High, 1, Level::Low, pulse_delay - 1))
            }))
            .await
            .unwrap();

        Ok(())
    }
}
