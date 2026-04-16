use embassy_time::Timer;
use esp_hal::delay::Delay;
use esp_hal::gpio::{Input, Output};
use futures::{FutureExt, select_biased};
use portable_atomic::{AtomicU32, Ordering};

use crate::cmd::{PumpCmd, PumpCmdSignal, ScaleCmdSignal, StopCmd, StopCmdSub};

// ---------------------------------------------------------------------------
// Calibration factor: raw counts per gram.
// Calibration is persisted by the host (Raspberry Pi) in hardware_config.json
// and restored at boot via G3.3 F{factor}.
// ---------------------------------------------------------------------------
const DEFAULT_COUNTS_PER_GRAM: f32 = 27.06;
static COUNTS_PER_GRAM: AtomicU32 = AtomicU32::new(DEFAULT_COUNTS_PER_GRAM.to_bits());

fn get_counts_per_gram() -> f32 {
    f32::from_bits(COUNTS_PER_GRAM.load(Ordering::Relaxed))
}

fn set_counts_per_gram(v: f32) {
    COUNTS_PER_GRAM.store(v.to_bits(), Ordering::Relaxed);
}

// ---------------------------------------------------------------------------
// Simple 1-D Kalman filter
// ---------------------------------------------------------------------------
struct Kalman {
    estimate: f32,
    error_cov: f32,
    process_noise: f32, // Q — how fast weight can change between samples
    meas_noise: f32,    // R — HX711 measurement noise variance
}

impl Kalman {
    fn new(initial: f32) -> Self {
        Self {
            estimate: initial,
            error_cov: 1000.0,
            process_noise: 500.0,
            meas_noise: 2500.0,
        }
    }

    fn update(&mut self, measurement: f32) -> f32 {
        let p_pred = self.error_cov + self.process_noise;
        let k = p_pred / (p_pred + self.meas_noise);
        self.estimate = self.estimate + k * (measurement - self.estimate);
        self.error_cov = (1.0 - k) * p_pred;
        self.estimate
    }
}

// ---------------------------------------------------------------------------
// HX711 low-level driver
// ---------------------------------------------------------------------------

/// Wait for HX711 DOUT to go low (data ready).
/// Returns false if it times out — chip not detected or not powered.
async fn wait_data_ready(data: &Input<'static>, timeout_ms: u64) -> bool {
    let deadline =
        embassy_time::Instant::now() + embassy_time::Duration::from_millis(timeout_ms);
    while data.is_high() {
        if embassy_time::Instant::now() >= deadline {
            return false;
        }
        Timer::after_millis(1).await;
    }
    true
}

/// Read one 24-bit sample from the HX711, channel A gain 128.
///
/// Uses blocking µs delays for bit-bang timing. CLK must never stay high >60µs
/// or the HX711 enters power-down. Async timers are far too coarse for this.
///
/// Returns None if the chip doesn't respond within 1 s.
async fn read_hx711(clk: &mut Output<'static>, data: &Input<'static>) -> Option<i32> {
    if !wait_data_ready(data, 1000).await {
        return None;
    }

    let delay = Delay::new();
    let mut value: i32 = 0;

    for _ in 0..24 {
        clk.set_high();
        delay.delay_micros(1);
        value = (value << 1) | if data.is_high() { 1 } else { 0 };
        clk.set_low();
        delay.delay_micros(1);
    }

    // 25th pulse — select channel A, gain 128 for next conversion
    clk.set_high();
    delay.delay_micros(1);
    clk.set_low();
    delay.delay_micros(1);

    // Sign-extend 24-bit → 32-bit
    if value & 0x80_0000 != 0 {
        value |= !0x00FF_FFFF;
    }

    Some(value)
}

// ---------------------------------------------------------------------------
// Scale task
// ---------------------------------------------------------------------------

/// G-code commands handled here:
///   G3       — read and report current weight
///   G3.1     — tare (zero) the scale
///   G3.2 W{g}— calibrate: current reading represents W grams (after tare)
///   G4 I{n} W{g} — fill using pump n until weight decreases by W grams
#[embassy_executor::task]
pub async fn scale_task(
    cmd_sig: &'static ScaleCmdSignal,
    pump_sig: &'static PumpCmdSignal,
    mut stop_sub: StopCmdSub,
    mut clk: Output<'static>,
    data: Input<'static>,
) {
    let mut tare_offset: i32 = 0;

    // Power up HX711 (CLK low = active mode)
    clk.set_low();

    // Startup probe
    if wait_data_ready(&data, 2000).await {
        log::info!("HX711 detected, scale ready\r");
        crate::led::set_error(crate::led::ErrorState::Ok);
    } else {
        log::error!("HX711 not detected! Check wiring (DATA=GPIO8, CLK=GPIO10)\r");
        crate::led::set_error(crate::led::ErrorState::HX711NotDetected);
    }

    loop {
        let cmd = cmd_sig.receive().await;

        match cmd.into_inner() {
            crate::cmd::ScaleCmd::Tare => match read_hx711(&mut clk, &data).await {
                Some(raw) => {
                    tare_offset = raw;
                    log::info!("Scale tared (offset: {})\r", tare_offset);
                }
                None => log::error!("HX711 not responding, cannot tare\r"),
            },

            crate::cmd::ScaleCmd::Read => match read_hx711(&mut clk, &data).await {
                Some(raw) => {
                    let weight_counts = raw - tare_offset;
                    let weight_g = weight_counts as f32 / get_counts_per_gram();
                    log::info!(
                        "Weight: {:.2}g (raw: {}, tared counts: {})\r",
                        weight_g,
                        raw,
                        weight_counts
                    );
                }
                None => log::error!("HX711 not responding, cannot read\r"),
            },

            crate::cmd::ScaleCmd::Calibrate { known_grams } => {
                match read_hx711(&mut clk, &data).await {
                    Some(raw) => {
                        let tared = (raw - tare_offset) as f32;
                        if tared.abs() < 1.0 {
                            log::error!("Calibration failed: reading too close to zero after tare. Tare first, then place known weight.\r");
                        } else {
                            let cpg = tared / known_grams;
                            set_counts_per_gram(cpg);
                            log::info!(
                                "Calibrated: {:.4} counts/gram (tared counts={:.0}, known={:.1}g)\r",
                                cpg, tared, known_grams
                            );
                        }
                    }
                    None => log::error!("HX711 not responding, cannot calibrate\r"),
                }
            }

            crate::cmd::ScaleCmd::SetFactor { factor } => {
                set_counts_per_gram(factor);
                log::info!("Scale calibration restored: {:.4} counts/gram\r", factor);
            }

            crate::cmd::ScaleCmd::Debug { samples } => {
                let n = samples.max(1) as usize;
                log::info!("[DBG] DOUT level before wait: {}\r", if data.is_high() { "HIGH (not ready)" } else { "LOW (ready/floating?)" });
                for i in 0..n {
                    let t0 = embassy_time::Instant::now();
                    let ready = wait_data_ready(&data, 1000).await;
                    let wait_ms = t0.elapsed().as_millis();
                    if !ready {
                        log::info!("[DBG] sample {} — wait_data_ready TIMED OUT after {}ms\r", i, wait_ms);
                        continue;
                    }
                    let delay = esp_hal::delay::Delay::new();
                    let mut value: i32 = 0;
                    for _ in 0..24 {
                        clk.set_high();
                        delay.delay_micros(1);
                        value = (value << 1) | if data.is_high() { 1 } else { 0 };
                        clk.set_low();
                        delay.delay_micros(1);
                    }
                    clk.set_high(); delay.delay_micros(1); clk.set_low(); delay.delay_micros(1);
                    if value & 0x80_0000 != 0 { value |= !0x00FF_FFFF; }
                    log::info!("[DBG] sample {} — wait={}ms raw={}\r", i, wait_ms, value);
                }
                log::info!("[DBG] done\r");
            }

            crate::cmd::ScaleCmd::Fill {
                pump_index,
                target_grams,
            } => {
                fill_loop(
                    pump_sig,
                    &mut stop_sub,
                    &mut clk,
                    &data,
                    tare_offset,
                    pump_index,
                    target_grams,
                )
                .await;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Fill loop — separated for readability
// ---------------------------------------------------------------------------

/// Overshoot compensation: stop pump when this fraction of target is reached.
/// Remaining liquid in tubing will account for the rest.
const STOP_FRACTION: f32 = 0.92;

/// Reject any single HX711 reading that deviates more than this from the Kalman estimate.
/// Catches brief EMI spikes, pump vibration artefacts, and the raw=-1 sentinel (-13 000 g).
const OUTLIER_THRESHOLD_G: f32 = 20.0;

/// If this many consecutive readings are all rejected in the *same direction*, the weight
/// has genuinely drifted and the filter is frozen. Force-reset the Kalman estimate to the
/// median of the buffered outlier readings so tracking resumes.
const OUTLIER_TREND_COUNT: usize = 8;

/// Allow this many consecutive HX711 read failures before aborting the fill.
const MAX_HX711_ERRORS: u32 = 5;

/// If weight change is less than this amount (grams), count it as no progress.
const MIN_WEIGHT_CHANGE_G: f32 = 0.5;

/// If no significant weight change for this many consecutive samples, abort (empty/blocked).
const NO_PROGRESS_LIMIT: u32 = 25;

/// Pump rate monitoring: check dispensing rate over a sliding 30-sample window.
const PUMP_RATE_WINDOW_SAMPLES: usize = 30;
const MIN_WEIGHT_CHANGE_PER_WINDOW_G: f32 = 25.0;

/// Maximum fill duration in milliseconds before aborting (safety net for empty bottles,
/// blocked tubing, or scale failure).
const FILL_TIMEOUT_MS: u64 = 30_000;

async fn fill_loop(
    pump_sig: &'static PumpCmdSignal,
    stop_sub: &mut StopCmdSub,
    clk: &mut Output<'static>,
    data: &Input<'static>,
    tare_offset: i32,
    pump_index: u8,
    target_grams: f32,
) {
    if target_grams <= 0.0 {
        log::info!("[FILL_END] reason=zero_target dispensed=0.0g duration=0ms\r");
        return;
    }

    let cpg = get_counts_per_gram();
    if cpg < 1.0 {
        log::error!("Scale not calibrated. Run G3.2 W{{grams}} first.\r");
        crate::led::set_error(crate::led::ErrorState::ScaleNotCalibrated);
        return;
    }

    // Baseline reading before pump starts
    let Some(baseline_raw) = read_hx711(clk, data).await else {
        log::error!("HX711 not responding, cannot start fill\r");
        return;
    };

    let baseline = (baseline_raw - tare_offset) as f32 / cpg;
    let stop_threshold = target_grams * STOP_FRACTION;
    let mut kalman = Kalman::new(baseline);

    // Start pump — long duration (60 s); will be cancelled when target reached
    pump_sig
        .send(PumpCmd {
            index: pump_index,
            duration_ms: 60_000,
            wait: false,
        })
        .await;

    let start = embassy_time::Instant::now();

    log::info!("[FILL_START] pump={} target={:.1}g stop_at={:.1}g\r",
        pump_index, target_grams, stop_threshold);
    log::info!("t_ms,raw,filtered_g,delta_g\r");

    let mut done = false;
    let mut consecutive_errors: u32 = 0;
    let mut last_dispensed_g: f32 = 0.0;
    let mut last_reported_weight: f32 = baseline;
    let mut no_progress_count: u32 = 0;
    // Ring buffer of recent outlier readings, used for trend detection.
    let mut outlier_buf = [0.0f32; OUTLIER_TREND_COUNT];
    let mut outlier_count: usize = 0;
    // Ring buffer for pump dispensing rate monitoring (15-sample window).
    let mut weight_history = [0.0f32; PUMP_RATE_WINDOW_SAMPLES];
    let mut history_idx = 0;
    let mut history_count = 0;

    loop {
        // Hard timeout — catches empty bottles, blocked tubing, or scale failure.
        let t_ms = start.elapsed().as_millis();
        if t_ms >= FILL_TIMEOUT_MS {
            pump_sig
                .send(PumpCmd { index: pump_index, duration_ms: 0, wait: false })
                .await;
            crate::led::set_error(crate::led::ErrorState::FillTimeout);
            log::warn!(
                "[FILL_END] reason=timeout dispensed={:.1}g duration={}ms\r",
                last_dispensed_g, t_ms
            );
            break;
        }
        // Wait for next HX711 reading OR emergency stop, whichever comes first.
        // Cancelling the read future mid-way is safe — Embassy drops it cleanly.
        let raw_opt = select_biased! {
            stop_cmd = stop_sub.next_message_pure().fuse() => {
                if stop_cmd != StopCmd::Continue {
                    done = true;
                }
                None
            },
            r = read_hx711(clk, data).fuse() => r,
        };

        if done {
            pump_sig
                .send(PumpCmd { index: pump_index, duration_ms: 0, wait: false })
                .await;
            log::info!("[FILL_END] reason=emergency_stop\r");
            break;
        }

        // HX711 returned None — allow several retries before aborting.
        let Some(raw) = raw_opt else {
            consecutive_errors += 1;
            log::warn!("[HX711] read error {}/{}\r", consecutive_errors, MAX_HX711_ERRORS);
            if consecutive_errors >= MAX_HX711_ERRORS {
                pump_sig
                    .send(PumpCmd { index: pump_index, duration_ms: 0, wait: false })
                    .await;
                crate::led::set_error(crate::led::ErrorState::HX711ReadError);
                log::error!("[FILL_END] reason=hx711_error\r");
                break;
            }
            continue;
        };

        consecutive_errors = 0;

        let weight_g = (raw - tare_offset) as f32 / cpg;

        // Outlier check: reject brief spikes/dips (EMI, vibration, raw=-1 sentinel).
        // However, if enough consecutive readings are all on the same side of the
        // estimate, the weight has genuinely drifted — reset the filter to catch up.
        if (weight_g - kalman.estimate).abs() > OUTLIER_THRESHOLD_G {
            outlier_buf[outlier_count % OUTLIER_TREND_COUNT] = weight_g;
            outlier_count += 1;

            if outlier_count >= OUTLIER_TREND_COUNT {
                // Check whether all buffered readings are on the same side.
                let all_above = outlier_buf.iter().all(|&v| v > kalman.estimate);
                let all_below = outlier_buf.iter().all(|&v| v < kalman.estimate);

                if all_above || all_below {
                    // Sustained trend — compute median of buffer and reset filter.
                    let mut sorted = outlier_buf;
                    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
                    let median = sorted[OUTLIER_TREND_COUNT / 2];
                    log::warn!(
                        "[TREND] t={}ms {} consecutive outliers {} estimate — resetting to {:.2}g\r",
                        t_ms,
                        outlier_count,
                        if all_above { "above" } else { "below" },
                        median
                    );
                    kalman = Kalman::new(median);
                    outlier_count = 0;
                    continue;
                }
            }

            log::warn!(
                "[OUTLIER] t={}ms raw={} measured={:.2}g estimate={:.2}g — skipped\r",
                t_ms, raw, weight_g, kalman.estimate
            );
            continue;
        }

        // Good reading — reset outlier tracking.
        outlier_count = 0;

        let filtered_g = kalman.update(weight_g);
        let delta_g = filtered_g - baseline; // negative as bottle empties
        last_dispensed_g = -delta_g;

        log::info!("{},{},{:.2},{:.2}\r", t_ms, raw, filtered_g, delta_g);

        // Check if weight is progressing (changing significantly)
        if (filtered_g - last_reported_weight).abs() < MIN_WEIGHT_CHANGE_G {
            no_progress_count += 1;
            if no_progress_count >= NO_PROGRESS_LIMIT {
                pump_sig
                    .send(PumpCmd { index: pump_index, duration_ms: 0, wait: false })
                    .await;
                crate::led::set_error(crate::led::ErrorState::PumpBlocked);
                log::error!(
                    "[FILL_END] reason=empty_or_blocked dispensed={:.1}g duration={}ms (no weight change for {} samples)\r",
                    last_dispensed_g, t_ms, no_progress_count
                );
                break;
            }
        } else {
            // Weight changed significantly, reset counter
            no_progress_count = 0;
            last_reported_weight = filtered_g;
        }

        // Pump rate monitoring: check if dispensing rate is adequate over 15-sample window.
        weight_history[history_idx % PUMP_RATE_WINDOW_SAMPLES] = filtered_g;
        history_idx += 1;
        if history_count < PUMP_RATE_WINDOW_SAMPLES {
            history_count += 1;
        }

        if history_count >= PUMP_RATE_WINDOW_SAMPLES {
            let oldest_weight = weight_history[history_idx % PUMP_RATE_WINDOW_SAMPLES];
            let weight_change = (oldest_weight - filtered_g).abs(); // Should be positive as weight decreases
            if weight_change < MIN_WEIGHT_CHANGE_PER_WINDOW_G {
                pump_sig
                    .send(PumpCmd { index: pump_index, duration_ms: 0, wait: false })
                    .await;
                crate::led::set_error(crate::led::ErrorState::PumpFailure);
                log::error!(
                    "[FILL_END] reason=pump_failure dispensed={:.1}g duration={}ms (insufficient dispensing rate: {:.1}g change in {} samples)\r",
                    last_dispensed_g, t_ms, weight_change, PUMP_RATE_WINDOW_SAMPLES
                );
                break;
            }
        }

        // Weight decreased by target amount? (delta is negative, so check -delta)
        if last_dispensed_g >= stop_threshold {
            pump_sig
                .send(PumpCmd { index: pump_index, duration_ms: 0, wait: false })
                .await;
            log::info!(
                "[FILL_END] reason=target_reached dispensed={:.1}g duration={}ms\r",
                last_dispensed_g, t_ms
            );
            break;
        }
    }
}
