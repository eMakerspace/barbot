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

use crate::cmd::{CmdChannel, ScaleCmdSignal, ServoCmdSignal, PumpCmdSignal, StepperCmdSignal, StopChannel};

esp_bootloader_esp_idf::esp_app_desc!();

pub mod cmd;
pub mod rmt;
pub mod stepgen;
pub mod stepper;
pub mod tasks;
pub mod utils;

extern crate alloc;

// Microstepping factor: hardware is configured for 1/4 steps.
pub const MICROSTEPS: f64 = 4.0;

pub const STEPPER_NORMAL_ACCEL_SPEED: stepper::AccelSpeedConfig = stepper::AccelSpeedConfig::zero()
    .with_acceleration(3000.0 * MICROSTEPS)
    .with_max_speed(3800.0 * MICROSTEPS);

const STEPPER_HOMING_ACCEL_SPEED: stepper::AccelSpeedConfig = stepper::AccelSpeedConfig::zero()
    .with_acceleration(8000.0 * MICROSTEPS)
    .with_max_speed(300.0 * MICROSTEPS);

static STOP_CHANNEL: StopChannel = StopChannel::new();
static STEPPER_CMD_SIG: StepperCmdSignal = StepperCmdSignal::new();
static CMD_CHANNEL: CmdChannel = CmdChannel::new();
static PUMP_CMD_SIG: PumpCmdSignal = PumpCmdSignal::new();
static SERVO_CMD_SIG: ServoCmdSignal = ServoCmdSignal::new();
static SCALE_CMD_SIG: ScaleCmdSignal = ScaleCmdSignal::new();

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
        peripherals.GPIO2,
        InputConfig::default().with_pull(esp_hal::gpio::Pull::Up),
    );
    spawner.must_spawn(tasks::emergency_stop_monitor(
        emergency_stop,
        STOP_CHANNEL.immediate_publisher(),
    ));
    
    let tx_cfg = rmt::TxChannelConfig {
        idle_output: true,
        idle_output_level: esp_hal::gpio::Level::Low,
        clk_divider: 40, // 1 microsecond tick (40 MHz / 40)
        memsize: 1,
        ..Default::default()
    };
    let mut rmt = rmt::Rmt::new(peripherals.RMT, Rate::from_mhz(40));
    let stepper_channel = rmt.channel(0, peripherals.GPIO20, tx_cfg);

    let mut stepper = Stepper::new(
        stepper_channel,
        Rate::from_hz(40_000_000 / 40),
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
        servo_sig: &SERVO_CMD_SIG,
        scale_sig: &SCALE_CMD_SIG,
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
    
    spawner.must_spawn(tasks::servo::servo_task(
        &SERVO_CMD_SIG,
        STOP_CHANNEL.subscriber().unwrap(),
        Output::new(peripherals.GPIO9, esp_hal::gpio::Level::Low, pump_pin_cfg),
    ));

    // HX711 scale: DATA on GPIO8, CLK on GPIO10
    let scale_data = Input::new(
        peripherals.GPIO8,
        InputConfig::default().with_pull(esp_hal::gpio::Pull::None),
    );
    let scale_clk = Output::new(
        peripherals.GPIO10,
        esp_hal::gpio::Level::Low,
        pump_pin_cfg,
    );
    spawner.must_spawn(tasks::scale::scale_task(
        &SCALE_CMD_SIG,
        &PUMP_CMD_SIG,
        STOP_CHANNEL.subscriber().unwrap(),
        scale_clk,
        scale_data,
    ));

    info!("Barbot HAT v{} running\r", env!("CARGO_PKG_VERSION"));
}
