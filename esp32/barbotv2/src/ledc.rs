use core::marker::PhantomData;

use esp_hal::ledc;
use esp_hal::peripherals::{self, LEDC};
use esp_hal::time::Rate;
use num_traits::Float;
use trouble_host::peripheral;

pub struct Ledc<'p> {
    _ledc: esp_hal::peripherals::LEDC<'p>,
}

impl<'p> Ledc<'p> {
    /// Create a new LEDC instance
    pub fn new(ledc: esp_hal::peripherals::LEDC<'p>) -> Self {
        // Enable APB clock for LEDC
        peripherals::SYSTEM::regs()
            .perip_clk_en0()
            .write(|w| w.ledc_clk_en().set_bit());

        // Configure clock source to APB
        LEDC::regs()
            .conf()
            .write(|w| unsafe { w.apb_clk_sel().bits(1) });
        Ledc { _ledc: ledc }
    }
}

pub struct TimerNum(pub u8);
impl TimerNum {

}

struct TimerConfig {
    /// Clock divider which divides the APB clock to produce the timer reference clock.
    ///
    /// The divider value is a fixed point number where the lower 8 bits are the fractional part.
    /// The resulting period of the divided timer will be distributed such that over 256 cycles
    /// the desired average frequency is achieved.
    divider: u32,
    /// Counter overflow value as a power of 2.
    ///
    /// The counter will count from 0 to (2^counter_overflow_pow2)-1 before wrapping around.
    /// This value must be between 0 and 14.
    counter_overflow_pow2: u8,
    /// High point value where the output signal goes high.
    ///
    /// This value must be less than `2^counter_overflow_pow2`.
    low_to_high_value: u16,
    /// Low point value where the output signal goes low.
    ///
    /// This value must be less than `2^counter_overflow_pow2`.
    high_to_low_value: u16,
}

impl Default for TimerConfig {
    fn default() -> Self {
        TimerConfig {
            divider: 256,                         // Divide by 1.0
            counter_overflow_pow2: 14,            // Max overflow
            low_to_high_value: 0,                 // Start of period
            high_to_low_value: (1_u16 << 13_u16), // 50% duty cycle
        }
    }
}

impl TimerConfig {
    /// Create a new TimerConfig with the specified counter overflow power of 2.
    pub fn new(counter_overflow_pow2: u8) -> Self {
        assert!(
            counter_overflow_pow2 <= 14,
            "Counter overflow power must be between 0 and 14"
        );
        TimerConfig {
            counter_overflow_pow2,
            ..Default::default()
        }
    }

    /// Set the output frequency of the timer
    pub fn with_frequency(mut self, freq: Rate) -> Self {
        let apb_clock = esp_hal::clock::Clocks::get().apb_clock.as_hz() as f32;
        let desired_freq = freq.as_hz() as f32;

        // Calculate the 18 bit fixed-point divider value where the 8 LSB are the fractional part.
        let divider = (apb_clock / desired_freq * 256.0).round() as u32;
        assert!(
            divider > 0 && divider < (1 << 18),
            "LEDC divider out of range"
        );

        self.divider = divider;
        self
    }

    /// Set the duty cycle of the timer output
    pub fn with_duty_cycle(mut self, duty_cycle: f32) -> Self {
        assert!(
            duty_cycle >= 0.0 && duty_cycle <= 1.0,
            "Duty cycle must be between 0.0 and 1.0"
        );

        let period = 1_u16 << self.counter_overflow_pow2;
        let high_time = (period as f32 * duty_cycle).round() as u16;

        self.low_to_high_value = 0;
        self.high_to_low_value = high_time;
        self
    }
}

pub struct LedcChannel<'p> {
    _ledc: PhantomData<LEDC<'p>>,
    timer: TimerNum,
}

impl<'p> LedcChannel<'p> {
    fn new(timer: TimerNum, cfg: TimerConfig) -> Self {
        let ledc = LEDC::regs();

        // TODO

        LedcChannel {
            _ledc: PhantomData,
            timer,
        }
    }
}
