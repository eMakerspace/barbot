use embassy_sync::blocking_mutex::raw::CriticalSectionRawMutex;
use embassy_sync::signal::Signal;
use esp_hal::gpio::{Level, Output, OutputConfig, OutputPin};
use esp_hal::rmt::PulseCode;
use esp_hal::time::Rate;

use crate::rmt::IterRmt;
use crate::stepgen::{self, Stepgen};

pub type StopSignal = Signal<CriticalSectionRawMutex, ()>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dir {
    /// Motor position increases.
    Positive,
    /// Motor position decreases.
    Negative,
}

impl Dir {
    /// Get the opposite direction.
    pub fn opposite(&self) -> Self {
        match self {
            Dir::Positive => Dir::Negative,
            Dir::Negative => Dir::Positive,
        }
    }

    /// Get the signed step count for the given amount of steps in this direction.
    pub fn for_steps(self, steps: u32) -> i32 {
        match self {
            Dir::Positive => steps as i32,
            Dir::Negative => -(steps as i32),
        }
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct AccelSpeedConfig {
    acceleration: u32,
    max_speed: u32,
}

impl AccelSpeedConfig {
    pub const fn new(acceleration: u32, max_speed: u32) -> Self {
        Self {
            acceleration,
            max_speed,
        }
    }

    /// Create a zeroed configuration.
    pub const fn zero() -> Self {
        Self {
            acceleration: 0,
            max_speed: 0,
        }
    }

    /// Set the acceleration in steps per second^2.
    pub const fn set_acceleration(&mut self, steps_per_second_sq: f64) {
        self.acceleration = (steps_per_second_sq * Stepper::DECIMALS_FACT) as u32;
    }

    /// Set the acceleration in steps per second^2.
    pub const fn with_acceleration(mut self, steps_per_second_sq: f64) -> Self {
        self.set_acceleration(steps_per_second_sq);
        self
    }

    /// Set the maximum speed in steps per second.
    pub const fn set_max_speed(&mut self, steps_per_second: f64) {
        self.max_speed = (steps_per_second * Stepper::DECIMALS_FACT) as u32;
    }

    /// Set the maximum speed in steps per second.
    pub const fn with_max_speed(mut self, steps_per_second: f64) -> Self {
        self.set_max_speed(steps_per_second);
        self
    }

    /// Validate the configuration for the given tick rate.
    fn validate(&self, ticks_per_second: u32) -> stepgen::Result {
        let mut gen = Stepgen::new(ticks_per_second);
        gen.set_acceleration(self.acceleration)?;
        gen.set_target_speed(self.max_speed)?;
        Ok(())
    }

    /// Validate the configuration for the given stepper motor.
    pub fn validate_for_stepper(&self, stepper: &Stepper) -> stepgen::Result {
        self.validate(stepper.tick_rate.as_hz())
    }
}

/// Stepper motor driver with absolute positioning and acceleration/deacceleration curve.
pub struct Stepper<'rmt> {
    rmt: IterRmt<'rmt>,
    dir_pin: Output<'static>,
    tick_rate: Rate,
    accel_speed: AccelSpeedConfig,
    stop_signal: Option<&'static StopSignal>,
    dir_high_positive: bool,
    curr_pos: i32,
    last_dir: Option<Dir>,
}

impl<'rmt> Stepper<'rmt> {
    pub const N_DECIMALS: u32 = 8;
    pub const DECIMALS_FACT: f64 = (1 << Self::N_DECIMALS) as f64;

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
        rmt: IterRmt<'rmt>,
        tick_rate: Rate,
        dir_pin: impl OutputPin + 'static,
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
            accel_speed: AccelSpeedConfig::default(),
            stop_signal: None,
            dir_high_positive,
            curr_pos: 0,
            last_dir: None,
        }
    }

    /// Get the current pos.
    pub fn curr_pos(&self) -> i32 {
        self.curr_pos
    }

    /// Set the current absolute motor step position.
    pub fn set_curr_pos(&mut self, curr_step: i32) {
        self.curr_pos = curr_step;
    }
    
    /// Offset the current absolute motor step position by the given amount of steps.
    pub fn offset_curr_pos(&mut self, offset_steps: i32) {
        self.curr_pos += offset_steps;
    }

    /// Get the last direction the motor was moving in, if any.
    pub fn last_dir(&self) -> Option<Dir> {
        self.last_dir
    }

    /// Set the stop signal, which when sent, will stop the motor.
    ///
    /// TODO: How fast should the motor deaccelerate? Currently, it does so with the
    /// configured acceleration.
    pub fn set_stop_signal(&mut self, stop_signal: &'static StopSignal) {
        self.stop_signal = Some(stop_signal);
    }

    /// Set the acceleration and maximum speed configuration.
    ///
    /// Validates the configuration before applying it, which makes sure the
    /// underlying step generator and timer can handle the requested values.
    pub fn set_accel_speed(&mut self, config: AccelSpeedConfig) -> stepgen::Result {
        config.validate_for_stepper(self)?;
        self.accel_speed = config;
        Ok(())
    }

    /// Convert a target position to direction and steps needed to reach it.
    pub fn dir_and_steps_to_pos(&self, pos: i32) -> (Dir, u32) {
        let steps = self.curr_pos.abs_diff(pos);
        let dir = if pos >= self.curr_pos {
            Dir::Positive
        } else {
            Dir::Negative
        };
        (dir, steps)
    }

    /// Convert a `Dir` to the corresponding `Level` for the dir pin.
    fn dir_to_level(&self, dir: Dir) -> Level {
        match (dir, self.dir_high_positive) {
            (Dir::Positive, true) | (Dir::Negative, false) => Level::High,
            (Dir::Positive, false) | (Dir::Negative, true) => Level::Low,
        }
    }

    /// Run to the given `pos` in steps.
    pub async fn run_to_pos(&mut self, pos: i32) {
        let (dir, steps) = self.dir_and_steps_to_pos(pos);
        let actual_steps = self.run_steps(steps, dir).await;

        self.curr_pos += if pos > self.curr_pos {
            actual_steps as i32
        } else {
            -(actual_steps as i32)
        };
    }

    /// Run the given amount of `steps` into the given direction `dir`
    /// while smoothly accelerating and deaccelerating.
    ///
    /// Returns on success the actual amount of steps run, which may be less
    /// if a stop signal was received.
    ///
    /// Note: Dropping the future returned by this method will stop the motor immediately
    /// (without deccelerating).
    async fn run_steps(&mut self, steps: u32, dir: Dir) -> u32 {
        let dir_level = self.dir_to_level(dir);
        self.last_dir = Some(dir);
        self.run_steps_with_level(steps, dir_level, self.accel_speed)
            .await
    }

    /// Run the given amount of `steps` into the given direction `dir`
    /// while smoothly accelerating and deaccelerating with the given acceleration and speed.
    ///
    /// Returns on success the actual amount of steps run, which may be less
    /// if a stop signal was received.
    ///
    /// Note: Dropping the future returned by this method will stop the motor immediately
    /// (without deccelerating).
    ///
    /// ## Panics
    /// - Panics if the RMT transmission fails.
    /// - Panics if the `accel_speed` configuration is not valid.
    pub async fn run_steps_with_accel_speed(
        &mut self,
        steps: u32,
        dir: Dir,
        accel_speed: AccelSpeedConfig,
    ) -> u32 {
        let dir_level = self.dir_to_level(dir);
        self.last_dir = Some(dir);
        self.run_steps_with_level(steps, dir_level, accel_speed)
            .await
    }

    /// Run the given amount of `steps` into the given `direction`
    /// while smoothly accelerating and deaccelerating.
    ///
    /// Returns the actual amount of steps run, which may be less if a stop signal was
    /// received.
    ///
    /// Note: Dropping the future returned by this method will stop the motor immediately
    /// (without deccelerating).
    ///
    /// ## Panics
    /// - Panics if the RMT transmission fails.
    /// - Panics if the `accel_speed` configuration is not valid.
    async fn run_steps_with_level(
        &mut self,
        steps: u32,
        dir_level: Level,
        accel_speed: AccelSpeedConfig,
    ) -> u32 {
        self.dir_pin.set_level(dir_level);

        let mut gen = Stepgen::new(self.tick_rate.as_hz());
        gen.set_acceleration(accel_speed.acceleration)
            .expect("invalid acceleration");
        gen.set_target_speed(accel_speed.max_speed)
            .expect("invalid max speed");
        gen.set_target_step(steps)
            .expect("speed and acceleration are configured");

        struct StepIter {
            gen: Stepgen,
            stop_signal: Option<&'static StopSignal>,
        }

        impl Iterator for StepIter {
            type Item = u32;

            fn next(&mut self) -> Option<Self::Item> {
                const MAX_PERIOD: u32 = (1 << 15) - 1;

                // Check for stop signal at the start of each step.
                if let Some(()) = self.stop_signal.and_then(|s| s.try_take()) {
                    self.stop_signal = None;
                    // Stop signal received, stop the motor by deccelerating with the
                    // configured acceleration.
                    self.gen
                        .set_target_step(0)
                        .expect("speed and acceleration are configured");
                }

                // Get the total step period in RMT ticks.
                let total_delay = self.gen.next()? >> 7;

                // Split the period of the step into an output high and low part.
                let second_period = total_delay.min(MAX_PERIOD - 1).max(1);
                let first_period = total_delay.saturating_sub(second_period).max(1);

                Some(
                    PulseCode::new(
                        Level::High,
                        first_period as u16,
                        Level::Low,
                        second_period as u16,
                    )
                    .into(),
                )
            }
        }

        let step_iter = StepIter {
            gen,
            stop_signal: self.stop_signal,
        };

        let iter = self
            .rmt
            .transmit(step_iter)
            .await
            .expect("rmt transaction failed");
        iter.gen.current_step()
    }
}
