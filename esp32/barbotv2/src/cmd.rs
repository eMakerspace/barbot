use embassy_sync::blocking_mutex::raw::CriticalSectionRawMutex;
use embassy_sync::channel::Channel;
use embassy_sync::pubsub::{self, PubSubChannel};

use crate::utils::BiSignal;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum StopCmd {
    Continue,
    Immediate,
    Graceful,
}

/// Channel for emergency stop commands.
///
/// `tasks::emergency_stop_monitor` publishes to this channel immediately when the emergency stop input is triggered.
/// `tasks::serial::serial_reader` can also publish to this channel when a stop command is received over serial.
/// All other tasks subscribe to this channel to receive emergency stop commands.
pub type StopChannel = PubSubChannel<CriticalSectionRawMutex, StopCmd, 1, 16, 1>;
pub type StopCmdSub = pubsub::Subscriber<'static, CriticalSectionRawMutex, StopCmd, 1, 16, 1>;
pub type StopCmdPub = pubsub::Publisher<'static, CriticalSectionRawMutex, StopCmd, 1, 16, 1>;
pub type StopCmdImmediatePub =
    pubsub::ImmediatePublisher<'static, CriticalSectionRawMutex, StopCmd, 1, 16, 1>;

#[derive(Debug)]
pub enum StepperCmd {
    GoTo(i32, Option<crate::stepper::AccelSpeedConfig>),
    GoToRangeFact(f32, Option<crate::stepper::AccelSpeedConfig>),
    Home(),
}
pub type StepperCmdSignal = BiSignal<StepperCmd>;

#[derive(Debug)]
pub struct PumpCmd {
    pub index: u8,
    pub duration_ms: u32,
    pub wait: bool,
}
pub type PumpCmdSignal = BiSignal<PumpCmd>;

#[derive(Debug)]
pub struct ServoCmd {
    pub angle: u8,
}
pub type ServoCmdSignal = BiSignal<ServoCmd>;

#[derive(Debug)]
pub enum ScaleCmd {
    Read,
    Tare,
    /// Fill using pump `pump_index` until weight decreases by `target_grams`.
    Fill { pump_index: u8, target_grams: f32 },
    /// Calibrate: current reading (after tare) represents `known_grams`.
    Calibrate { known_grams: f32 },
    /// Directly set the counts-per-gram factor (restored from Pi at boot via G3.3).
    SetFactor { factor: f32 },
    /// Debug: read N raw samples and report timing. Used by G3.9.
    Debug { samples: u8 },
}
pub type ScaleCmdSignal = BiSignal<ScaleCmd>;

#[derive(Debug)]
pub enum Cmd {
    Stepper(StepperCmd),
    Pump(PumpCmd),
    Servo(ServoCmd),
    Scale(ScaleCmd),
    Wait(u32),
}
pub type CmdChannel = Channel<CriticalSectionRawMutex, Cmd, 128>;
