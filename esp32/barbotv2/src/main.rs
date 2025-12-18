#![no_std]
#![no_main]

use embassy_executor::Spawner;
use esp_backtrace as _;
use esp_hal::clock::CpuClock;
use esp_hal::gpio::{self, Input, InputConfig, Output};
use esp_hal::time::Rate;
use esp_hal::usb_serial_jtag::UsbSerialJtag;
use log::info;
use stepper::Stepper;

use crate::cmd::{CmdChannel, LiftMotorCmdSignal, PumpCmdSignal, StepperCmdSignal, StopChannel};

esp_bootloader_esp_idf::esp_app_desc!();

pub mod cmd;
pub mod rmt;
pub mod stepgen;
pub mod stepper;
pub mod tasks;
pub mod utils;

extern crate alloc;

// Note: the stepper driver operates in 1/8 th steps. Therefore, 8 microsteps = 1 motor step.
const STEPPER_NORMAL_ACCEL_SPEED: stepper::AccelSpeedConfig = stepper::AccelSpeedConfig::zero()
    .with_acceleration(1000.0 * 8.0) // 1.5 turns / seccond^2 (in simulation, 200 steps / turn)
    .with_max_speed(2000.0 * 8.0); // 1.0 turns / seccond (in simulation, 200 steps / turn)

const STEPPER_HOMING_ACCEL_SPEED: stepper::AccelSpeedConfig = stepper::AccelSpeedConfig::zero()
    .with_acceleration(8000.0 * 8.0) // 1.5 turns / seccond^2 (in simulation, 200 steps / turn)
    .with_max_speed(300.0 * 8.0); // 0.1 turns / seccond (in simulation, 200 steps / turn)

static STOP_CHANNEL: StopChannel = StopChannel::new();
static STEPPER_CMD_SIG: StepperCmdSignal = StepperCmdSignal::new();
static CMD_CHANNEL: CmdChannel = CmdChannel::new();
static PUMP_CMD_SIG: PumpCmdSignal = PumpCmdSignal::new();
static LIFT_MOTOR_CMD_SIG: LiftMotorCmdSignal = LiftMotorCmdSignal::new();

#[esp_rtos::main]
async fn main(spawner: Spawner) {
    esp_println::logger::init_logger_from_env();

    let config = esp_hal::Config::default().with_cpu_clock(CpuClock::max());
    let peripherals = esp_hal::init(config);
    esp_alloc::heap_allocator!(size: 72 * 1024);

    let timg0 = esp_hal::timer::timg::TimerGroup::new(peripherals.TIMG0);
    let sw_interrupt =
        esp_hal::interrupt::software::SoftwareInterruptControl::new(peripherals.SW_INTERRUPT);
    esp_rtos::start(timg0.timer0, sw_interrupt.software_interrupt0);

    info!("Startup...\r");

    // let timer1 = TimerGroup::new(peripherals.TIMG0);
    // let _init = esp_wifi::init(
    //     timer1.timer0,
    //     esp_hal::rng::Rng::new(peripherals.RNG),
    //     peripherals.RADIO_CLK,
    // )
    // .unwrap();
    //

    let (usb_rx, _) = UsbSerialJtag::new(peripherals.USB_DEVICE)
        .into_async()
        .split();
    spawner.must_spawn(tasks::serial::serial_reader(
        usb_rx,
        &CMD_CHANNEL,
        STOP_CHANNEL.publisher().unwrap(),
    ));

    let emergency_stop = Input::new(
        peripherals.GPIO9,
        InputConfig::default().with_pull(esp_hal::gpio::Pull::Up),
    );
    spawner.must_spawn(tasks::emergency_stop_monitor(
        emergency_stop,
        STOP_CHANNEL.immediate_publisher(),
    ));
    
    let tx_cfg = rmt::TxChannelConfig {
        idle_output: true,
        idle_output_level: esp_hal::gpio::Level::Low,
        clk_divider: 80, // 2 microsecond tick (40 MHz / 80)
        memsize: 1,
        ..Default::default()
    };
    let mut rmt = rmt::Rmt::new(peripherals.RMT, Rate::from_mhz(40));
    let stepper_channel = rmt.channel(0, peripherals.GPIO20, tx_cfg);

    let mut stepper = Stepper::new(
        stepper_channel,
        Rate::from_hz(40_000_000 / 80),
        peripherals.GPIO21,
        false,
    );
    stepper.set_accel_speed(STEPPER_NORMAL_ACCEL_SPEED).unwrap();
    STEPPER_HOMING_ACCEL_SPEED
        .validate_for_stepper(&stepper)
        .unwrap();

    let end_switch = Input::new(
        peripherals.GPIO7,
        InputConfig::default().with_pull(esp_hal::gpio::Pull::Up),
    );
    spawner.must_spawn(tasks::stepper::stepper_task(
        stepper,
        end_switch,
        &STEPPER_CMD_SIG,
        STOP_CHANNEL.subscriber().unwrap(),
        spawner,
    ));

    spawner.must_spawn(tasks::route_cmd::route_cmd(tasks::route_cmd::HandleCmd {
        cmd_chan: &CMD_CHANNEL,
        stepper_sig: &STEPPER_CMD_SIG,
        pump_sig: &PUMP_CMD_SIG,
        stop_sub: STOP_CHANNEL.subscriber().unwrap(),
        lift_motor_sig: &LIFT_MOTOR_CMD_SIG,
    }));

    let pump_pin_cfg = gpio::OutputConfig::default().with_drive_mode(gpio::DriveMode::PushPull);
    spawner.must_spawn(tasks::pump::pump_task(
        &PUMP_CMD_SIG,
        STOP_CHANNEL.subscriber().unwrap(),
        [
            Output::new(peripherals.GPIO6, tasks::pump::INACTIVE_LEVEL, pump_pin_cfg),
            Output::new(peripherals.GPIO5, tasks::pump::INACTIVE_LEVEL, pump_pin_cfg),
            Output::new(peripherals.GPIO4, tasks::pump::INACTIVE_LEVEL, pump_pin_cfg),
            Output::new(peripherals.GPIO3, tasks::pump::INACTIVE_LEVEL, pump_pin_cfg),
        ],
    ));
    
    spawner.must_spawn(
        tasks::lift_motor::lift_motor(
            &LIFT_MOTOR_CMD_SIG,
            STOP_CHANNEL.subscriber().unwrap(),
            Output::new(peripherals.GPIO10, tasks::lift_motor::INACTIVE_LEVEL, pump_pin_cfg),
            Output::new(peripherals.GPIO8, tasks::lift_motor::INACTIVE_LEVEL, pump_pin_cfg),
        )
    );

    info!("Barbot HAT v{} running\r", env!("CARGO_PKG_VERSION"));
}
