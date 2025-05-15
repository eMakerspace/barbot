#![no_std]
#![no_main]

use core::sync::atomic::{AtomicBool, Ordering};

use embassy_executor::Spawner;
use embassy_sync::blocking_mutex::raw::{CriticalSectionRawMutex, NoopRawMutex};
use embassy_sync::channel::{Channel, Sender};
use embassy_sync::signal::Signal;
use embassy_time::{Duration, Timer};
use embedded_io_async::Read;
use esp_backtrace as _;
use esp_hal::clock::CpuClock;
use esp_hal::gpio::{Input, InputConfig};
use esp_hal::time::Rate;
use esp_hal::timer::systimer::SystemTimer;
use esp_hal::usb_serial_jtag::{UsbSerialJtag, UsbSerialJtagRx};
use esp_println::{print, println};
use esp_wifi::wifi::event::HomeChannelChange;
use futures::{select_biased, FutureExt};
use log::info;
use num_traits::float::FloatCore;
use num_traits::ToPrimitive;
use stepper::Stepper;

pub mod rmt;
pub mod stepgen;
pub mod stepper;

extern crate alloc;

#[derive(Debug)]
enum StepperCmd {
    Stop { force: bool },
    GoTo(i32),
    Home(),
}

type StepperChannel = Channel<CriticalSectionRawMutex, StepperCmd, 1>;

static SHOULD_ECHO: AtomicBool = AtomicBool::new(true);

async fn handle_cmd(cmd: &str, stepper_chan: &'static StepperChannel) {
    let gcmd = match gcode::parse(cmd).next() {
        Some(cmd) => cmd,
        None => {
            log::warn!("received cmd '{cmd}' is not valid gcode");
            return;
        }
    };

    let invalid_cmd = || {
        log::error!("invalid command: {gcmd}");
    };

    match gcmd.mnemonic() {
        gcode::Mnemonic::General => match gcmd.major_number() {
            // `G0 X{position}` Move stepper motor to step {position}.
            0 => {
                if let Some(loc) = gcmd.value_for('X') {
                    let Some(loc) = (loc * 8.0).round().to_i32() else {
                        invalid_cmd();
                        return;
                    };

                    stepper_chan.send(StepperCmd::GoTo(loc)).await;
                }
            }
            // `G28` start homing.
            28 => {
                stepper_chan.send(StepperCmd::Home()).await;
            }
            _ => invalid_cmd(),
        },
        gcode::Mnemonic::Miscellaneous => match gcmd.major_number() {
            // Stop the stepper motor.
            // - `M0` stops slowly.
            // - `M0.1` stops immediately.
            0 => {
                stepper_chan
                    .send(StepperCmd::Stop {
                        force: gcmd.minor_number() == 1,
                    })
                    .await;
            }
            // `M10` Toggle local echo.
            10 => {
                SHOULD_ECHO.store(!SHOULD_ECHO.load(Ordering::Relaxed), Ordering::SeqCst);
            }
            _ => invalid_cmd(),
        },
        _ => invalid_cmd(),
    }
}

/// Read from the USB serial and execute the commands
#[embassy_executor::task]
async fn serial_reader(
    mut rx: UsbSerialJtagRx<'static, esp_hal::Async>,
    stepper_chan: &'static StepperChannel,
) {
    let mut buf = [0_u8; 128];

    let mut total_read: usize = 0;
    let mut ignore_next_command = false;
    loop {
        let count_read = match rx.read(&mut buf[total_read..]).await {
            Err(err) => {
                log::error!("{err}");
                continue;
            }
            Ok(val) => val,
        };

        // Echo.
        let echo = SHOULD_ECHO.load(Ordering::Relaxed);
        if echo {
            esp_println::Printer::write_bytes(&buf[total_read..total_read + count_read]);
        }

        if let Some((idx, c)) = buf[total_read..total_read + count_read]
            .iter()
            .enumerate()
            .find(|(_i, &c)| c == b'\r' || c == b'\n')
        {
            if echo {
                // Echo the appropriate missing character so that we echo a CRLF or LFCR,
                // for a true linebreak.
                if *c == b'\n' {
                    print!("\r");
                } else if total_read + idx + 1 == total_read + count_read {
                    print!("\n");
                }
            }

            total_read += idx;
            let s = match core::str::from_utf8(&buf[..total_read]) {
                Ok(s) => s,
                Err(_) => {
                    log::warn!("invalid utf-8 received");
                    continue;
                }
            };

            if !ignore_next_command {
                handle_cmd(s.trim(), stepper_chan).await;
            }
            ignore_next_command = false;

            let range_start = total_read + 1;
            let range_end = total_read - idx + count_read;
            buf.copy_within(range_start..range_end, 0);
            total_read = count_read - idx - 1;
        } else {
            total_read += count_read;
        }

        if total_read == buf.len() {
            log::warn!("ignoring command, buffer full");
            total_read = 0;
            ignore_next_command = true;
        }
    }
}

#[embassy_executor::task]
async fn stepper_task(
    mut stepper: Stepper,
    mut end_stop: Input<'static>,
    stepper_chan: &'static StepperChannel,
) {
    static STOP_SIGNAL: stepper::StopSignal = stepper::StopSignal::new();
    static CMD_SIGNAL: Signal<CriticalSectionRawMutex, StepperCmd> = Signal::new();
    static FORCE_STOP_SIGNAL: Signal<CriticalSectionRawMutex, ()> = Signal::new();

    #[embassy_executor::task]
    async fn stepper_signal_task(
        stop_sig: &'static stepper::StopSignal,
        force_stop_sig: &'static Signal<CriticalSectionRawMutex, ()>,
        cmd_sig: &'static Signal<CriticalSectionRawMutex, StepperCmd>,
        input_sig: &'static StepperChannel,
    ) {
        loop {
            match input_sig.receive().await {
                StepperCmd::Stop { force } => {
                    if force {
                        stop_sig.signal(());
                        force_stop_sig.signal(());
                    } else {
                        log::info!("Stopping early from command");
                        stop_sig.signal(());
                    }
                }
                cmd => {
                    if cmd_sig.signaled() {
                        log::warn!("Stepper busy, dropping cmd: {cmd:?}");
                        continue;
                    }
                    cmd_sig.signal(cmd);
                }
            }
        }
    }

    embassy_executor::Spawner::for_current_executor()
        .await
        .must_spawn(stepper_signal_task(
            &STOP_SIGNAL,
            &FORCE_STOP_SIGNAL,
            &CMD_SIGNAL,
            stepper_chan,
        ));

    async fn home_stepper(stepper: &mut Stepper, end_stop: &mut Input<'static>) -> bool {
        // TODO
        false
    }

    let mut homing_needed = false;
    stepper.set_stop_signal(&STOP_SIGNAL);

    loop {
        let pos = match CMD_SIGNAL.wait().await {
            StepperCmd::GoTo(pos) => {
                if !homing_needed {
                    pos
                } else {
                    log::warn!("Ignoring command, homing required");
                    continue;
                }
            }
            StepperCmd::Home() => {
                log::info!("Homing..");
                homing_needed = home_stepper(&mut stepper, &mut end_stop).await;
                continue;
            }
            _ => unreachable!(),
        };

        let res = select_biased! {
            _ = end_stop.wait_for_low().fuse() => {
                log::warn!("End stop reached, stopping immediately");
                STOP_SIGNAL.reset();
                FORCE_STOP_SIGNAL.reset();
                homing_needed = false;
                stepper.set_curr_pos(0);
                continue;
            },
            _ = FORCE_STOP_SIGNAL.wait().fuse() => {
                log::warn!("Stopping immediately from command");
                STOP_SIGNAL.reset();
                homing_needed = true;
                continue;
            },
            res = stepper.run_to_pos(pos).fuse() => res
        };
        if let Err(err) = res {
            log::warn!("Stepper moving failed: {err:?}");
            continue;
        }
    }
}

#[esp_hal_embassy::main]
async fn main(spawner: Spawner) {
    esp_println::logger::init_logger_from_env();

    let config = esp_hal::Config::default().with_cpu_clock(CpuClock::max());
    let peripherals = esp_hal::init(config);
    esp_alloc::heap_allocator!(size: 72 * 1024);

    let timer0 = SystemTimer::new(peripherals.SYSTIMER);
    esp_hal_embassy::init(timer0.alarm0);

    info!("Startup...");

    // let timer1 = TimerGroup::new(peripherals.TIMG0);
    // let _init = esp_wifi::init(
    //     timer1.timer0,
    //     esp_hal::rng::Rng::new(peripherals.RNG),
    //     peripherals.RADIO_CLK,
    // )
    // .unwrap();
    //
    static STEPPER_CHANNEL: StepperChannel = StepperChannel::new();

    let (usb_rx, _) = UsbSerialJtag::new(peripherals.USB_DEVICE)
        .into_async()
        .split();

    spawner.must_spawn(serial_reader(usb_rx, &STEPPER_CHANNEL));

    info!("Barbot HAT v{} running", env!("CARGO_PKG_VERSION"));

    let tx_cfg = esp_hal::rmt::TxChannelConfig::default()
        .with_idle_output(true)
        .with_idle_output_level(esp_hal::gpio::Level::Low)
        .with_clk_divider(160); // 4 microsecond tick (40 MHz / 160)
    let rmt = rmt::IterRmt::new(
        peripherals.RMT,
        Rate::from_mhz(40),
        peripherals.GPIO20,
        tx_cfg,
    );
    let mut stepper = Stepper::new(
        rmt,
        Rate::from_hz(40_000_000 / 160),
        peripherals.GPIO21,
        false,
    );

    // Note: the stepper driver operates in 1/8 th steps. Therefore, 8 microsteps = 1 motor step.
    stepper.set_acceleration(300.0 * 8.0); // 1.5 turns / seccond^2 (in simulation, 200 steps / turn)
    stepper.set_max_speed(200.0 * 8.0); // 1.0 turns / seccond (in simulation, 200 steps / turn)

    let end_stop = Input::new(
        peripherals.GPIO10,
        InputConfig::default().with_pull(esp_hal::gpio::Pull::Up),
    );
    spawner.must_spawn(stepper_task(stepper, end_stop, &STEPPER_CHANNEL));
}
