#![no_std]
#![no_main]

use embassy_executor::Spawner;
use embassy_time::{Duration, Timer};
use embedded_io_async::Read;
use esp_backtrace as _;
use esp_hal::clock::CpuClock;
use esp_hal::timer::systimer::SystemTimer;
use esp_hal::usb_serial_jtag::{UsbSerialJtag, UsbSerialJtagRx};
use log::info;

extern crate alloc;

async fn handle_cmd(cmd: &str) {
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

        if let Some((idx, _)) = buf[total_read..total_read+count_read]
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
                log::info!("cmd: {s}");
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
}
