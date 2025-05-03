#![no_std]
#![no_main]

use embassy_executor::Spawner;
use embassy_time::{Duration, Timer};
use embedded_io_async::Read;
use esp_backtrace as _;
use esp_hal::clock::CpuClock;
use esp_hal::gpio::AnyPin;
use esp_hal::interrupt::{InterruptHandler, Priority};
use esp_hal::peripheral::Peripheral;
use esp_hal::peripherals::RMT;
use esp_hal::rmt::{Rmt, TxChannelCreator};
use esp_hal::time::Rate;
use esp_hal::timer::systimer::SystemTimer;
use esp_hal::usb_serial_jtag::{UsbSerialJtag, UsbSerialJtagRx};
use log::info;

pub mod stepper;

extern crate alloc;

async fn handle_cmd(cmd: &str) {
    let gcmd = match gcode::parse(cmd).next() {
        Some(cmd) => cmd,
        None => {
            log::warn!("received cmd '{cmd}' is not valid gcode");
            return;
        }
    };

    // TODO: do something with command
}

/// Read from the USB serial and execute the commands
#[embassy_executor::task]
async fn serial_reader(mut rx: UsbSerialJtagRx<'static, esp_hal::Async>) {
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

        if let Some((idx, _)) = buf[total_read..total_read + count_read]
            .iter()
            .enumerate()
            .find(|(_i, &c)| c == b'\r' || c == b'\n')
        {
            total_read += idx;
            let s = match core::str::from_utf8(&buf[..total_read]) {
                Ok(s) => s,
                Err(_) => {
                    log::warn!("invalid utf-8 received");
                    continue;
                }
            };

            if !ignore_next_command {
                handle_cmd(s.trim()).await;
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

    let (usb_rx, _) = UsbSerialJtag::new(peripherals.USB_DEVICE)
        .into_async()
        .split();

    spawner
        .spawn(serial_reader(usb_rx))
        .expect("serial reader task");

    info!("Barbot HAT v{} running", env!("CARGO_PKG_VERSION"));
}

struct IterRmt<C: esp_hal::rmt::TxChannelInternal> {
    rmt: C,
}

impl<C: esp_hal::rmt::TxChannelInternal> IterRmt<C> {
    pub fn new(rmt_periph: impl Peripheral<P = RMT>, gpio: AnyPin) {
        use esp_hal::interrupt::InterruptConfigurable;
        use esp_hal::rmt::{Channel, Event, TxChannelInternal};
        use esp_hal::Blocking;

        let mut rmt = Rmt::new(rmt_periph, Rate::from_mhz(80)).unwrap();
        let tx_cfg = esp_hal::rmt::TxChannelConfig::default()
            .with_idle_output(true)
            .with_idle_output_level(esp_hal::gpio::Level::Low)
            .with_clk_divider(160); // 2 microsecond pulse

        extern "C" fn interrupt_handler() {
            let st = RMT::regs().int_st().read();

            if st.ch0_tx_end().bit() || st.ch0_tx_err().bit() {
                Channel::<Blocking, 0>::clear_interrupts();
                // TODO(Dominik): finish
            }
        }

        rmt.set_interrupt_handler(InterruptHandler::new(interrupt_handler, Priority::None));
        let rmt = rmt.channel0.configure(gpio, tx_cfg).unwrap();

        Channel::<Blocking, 0>::enable_listen_interrupt(
            Event::Error | Event::Threshold | Event::End,
            true,
        );

        unsafe {
            esp_hal::peripherals::RMT::regs()
                .ch0_tx_conf0()
                .modify(|_, w| w.mem_size().bits(2));
        }
        
        // TODO(Dominik): finish
    }
}
