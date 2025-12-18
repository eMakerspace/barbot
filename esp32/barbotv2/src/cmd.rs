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
    GoTo(i32),
    GoToRangeFact(f32),
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
pub struct LiftMotorCmd {
    pub direction_up: bool,
    pub duration_ms: u32,
}
pub type LiftMotorCmdSignal = BiSignal<LiftMotorCmd>;

#[derive(Debug)]
pub enum Cmd {
    Stepper(StepperCmd),
    Pump(PumpCmd),
    Led(),
    LiftMotor(LiftMotorCmd),
    Wait(u32),
}
pub type CmdChannel = Channel<CriticalSectionRawMutex, Cmd, 128>;
