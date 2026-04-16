use portable_atomic::{AtomicU8, Ordering};

/// Error states that trigger LED effects
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum ErrorState {
    // Status states (0-5)
    Ok = 0,                          // Green idle
    Ready = 1,                        // Green steady
    Warning = 2,                      // Yellow steady

    // Critical errors (10-19)
    HX711NotDetected = 10,
    CupMissing = 11,
    StepperHomingFailed = 12,
    EmergencyStop = 13,
    LowPower = 14,
    WatchdogTimeout = 15,

    // High errors (20-29)
    PumpFailure = 20,
    PumpBlocked = 21,
    FillTimeout = 22,
    ServoForbiddenZone = 23,
    StepperOutOfBounds = 24,
    HX711ReadError = 25,

    // Medium errors (30-39)
    WeightOutlier = 30,
    ScaleNotCalibrated = 31,
    CupRemoved = 32,
    StepperPositionMismatch = 33,

    // Communication errors (40-49)
    SerialBufferOverflow = 40,
    InvalidGcode = 41,
    InvalidParameter = 42,
    UnknownCommand = 43,
    SerialTimeout = 44,
}

impl ErrorState {
    /// Convert u8 code to ErrorState (safe because we validate)
    fn from_code(code: u8) -> Self {
        match code {
            0 => ErrorState::Ok,
            1 => ErrorState::Ready,
            2 => ErrorState::Warning,
            10 => ErrorState::HX711NotDetected,
            11 => ErrorState::CupMissing,
            12 => ErrorState::StepperHomingFailed,
            13 => ErrorState::EmergencyStop,
            14 => ErrorState::LowPower,
            15 => ErrorState::WatchdogTimeout,
            20 => ErrorState::PumpFailure,
            21 => ErrorState::PumpBlocked,
            22 => ErrorState::FillTimeout,
            23 => ErrorState::ServoForbiddenZone,
            24 => ErrorState::StepperOutOfBounds,
            25 => ErrorState::HX711ReadError,
            30 => ErrorState::WeightOutlier,
            31 => ErrorState::ScaleNotCalibrated,
            32 => ErrorState::CupRemoved,
            33 => ErrorState::StepperPositionMismatch,
            40 => ErrorState::SerialBufferOverflow,
            41 => ErrorState::InvalidGcode,
            42 => ErrorState::InvalidParameter,
            43 => ErrorState::UnknownCommand,
            44 => ErrorState::SerialTimeout,
            _ => ErrorState::Ok, // Default to Ok for unknown codes
        }
    }

    /// Convert error state to LED effect name (sent to Pi for NeoPixel control)
    pub fn to_led_effect(&self) -> &'static str {
        match self {
            // Status states — Green
            ErrorState::Ok => "IDLE",
            ErrorState::Ready => "GREEN_SOLID",

            // Critical errors — Red flashing fast
            ErrorState::HX711NotDetected => "RED_FLASH_FAST",
            ErrorState::CupMissing => "RED_FLASH_FAST",
            ErrorState::StepperHomingFailed => "RED_FLASH_FAST",
            ErrorState::EmergencyStop => "RED_PULSE",
            ErrorState::LowPower => "RED_FLASH_FAST",
            ErrorState::WatchdogTimeout => "RED_FLASH_FAST",

            // High errors — Red solid
            ErrorState::PumpFailure => "RED_SOLID",
            ErrorState::PumpBlocked => "RED_SOLID",
            ErrorState::FillTimeout => "RED_SOLID",
            ErrorState::ServoForbiddenZone => "RED_SOLID",
            ErrorState::StepperOutOfBounds => "RED_SOLID",
            ErrorState::HX711ReadError => "RED_SOLID",

            // Medium errors — Yellow solid
            ErrorState::WeightOutlier => "YELLOW_SOLID",
            ErrorState::ScaleNotCalibrated => "YELLOW_SOLID",
            ErrorState::CupRemoved => "YELLOW_SOLID",
            ErrorState::StepperPositionMismatch => "YELLOW_SOLID",

            // Communication errors — Orange flashing
            ErrorState::SerialBufferOverflow => "ORANGE_FLASH",
            ErrorState::InvalidGcode => "ORANGE_FLASH",
            ErrorState::InvalidParameter => "ORANGE_FLASH",
            ErrorState::UnknownCommand => "ORANGE_FLASH",
            ErrorState::SerialTimeout => "ORANGE_FLASH",

            ErrorState::Warning => "YELLOW_SOLID",
        }
    }

    /// Get severity level (0=info, 1=warning, 2=error, 3=critical)
    pub fn severity(&self) -> u8 {
        match self {
            ErrorState::Ok | ErrorState::Ready => 0,
            ErrorState::Warning => 1,
            ErrorState::WeightOutlier | ErrorState::ScaleNotCalibrated
            | ErrorState::CupRemoved | ErrorState::StepperPositionMismatch => 2,
            ErrorState::PumpFailure | ErrorState::PumpBlocked | ErrorState::FillTimeout
            | ErrorState::ServoForbiddenZone | ErrorState::StepperOutOfBounds
            | ErrorState::HX711ReadError
            | ErrorState::SerialBufferOverflow | ErrorState::InvalidGcode
            | ErrorState::InvalidParameter | ErrorState::UnknownCommand
            | ErrorState::SerialTimeout => 2,
            ErrorState::HX711NotDetected | ErrorState::CupMissing | ErrorState::StepperHomingFailed
            | ErrorState::EmergencyStop | ErrorState::LowPower | ErrorState::WatchdogTimeout => 3,
        }
    }
}

/// Global error state tracker
static CURRENT_ERROR: AtomicU8 = AtomicU8::new(ErrorState::Ok as u8);

/// Set the current error state and return the previous state
pub fn set_error(state: ErrorState) -> ErrorState {
    let prev = CURRENT_ERROR.swap(state as u8, Ordering::Relaxed);

    // Only log if this is a new error
    if prev != state as u8 {
        log::error!(
            "[ERROR_STATE] code={} name={} effect={} severity={}\r",
            state as u8,
            state_name(state),
            state.to_led_effect(),
            state.severity()
        );
    }

    // Safe because we only store valid ErrorState values
    ErrorState::from_code(prev)
}

/// Get the current error state
pub fn get_error() -> ErrorState {
    let code = CURRENT_ERROR.load(Ordering::Relaxed);
    ErrorState::from_code(code)
}

/// Clear error and return to Ok state
pub fn clear_error() -> ErrorState {
    set_error(ErrorState::Ok)
}

/// Check if we're in a critical error state
pub fn is_critical() -> bool {
    let err = get_error();
    err.severity() >= 3
}

/// Check if we're in an error state (any error, not Ok)
pub fn has_error() -> bool {
    get_error() != ErrorState::Ok
}

/// Get human-readable name for error state
fn state_name(state: ErrorState) -> &'static str {
    match state {
        ErrorState::Ok => "System OK",
        ErrorState::Ready => "System Ready",
        ErrorState::Warning => "Warning",
        ErrorState::HX711NotDetected => "HX711 Not Detected",
        ErrorState::CupMissing => "Cup Missing",
        ErrorState::StepperHomingFailed => "Stepper Homing Failed",
        ErrorState::EmergencyStop => "Emergency Stop",
        ErrorState::LowPower => "Low Power",
        ErrorState::WatchdogTimeout => "Watchdog Timeout",
        ErrorState::PumpFailure => "Pump Failure",
        ErrorState::PumpBlocked => "Pump Blocked",
        ErrorState::FillTimeout => "Fill Timeout",
        ErrorState::ServoForbiddenZone => "Servo Forbidden Zone",
        ErrorState::StepperOutOfBounds => "Stepper Out of Bounds",
        ErrorState::HX711ReadError => "HX711 Read Error",
        ErrorState::WeightOutlier => "Weight Outlier",
        ErrorState::ScaleNotCalibrated => "Scale Not Calibrated",
        ErrorState::CupRemoved => "Cup Removed",
        ErrorState::StepperPositionMismatch => "Stepper Position Mismatch",
        ErrorState::SerialBufferOverflow => "Serial Buffer Overflow",
        ErrorState::InvalidGcode => "Invalid G-code",
        ErrorState::InvalidParameter => "Invalid Parameter",
        ErrorState::UnknownCommand => "Unknown Command",
        ErrorState::SerialTimeout => "Serial Timeout",
    }
}
