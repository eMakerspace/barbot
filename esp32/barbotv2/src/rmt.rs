use core::future::Future;
use core::iter::{FusedIterator, Once};
use core::ptr::NonNull;
use core::sync::atomic::AtomicPtr;

use enumset::EnumSet;
use esp_hal::gpio::interconnect::PeripheralOutput;
use core::iter::{Chain, Fuse};
use esp_hal::asynch::AtomicWaker;
use esp_hal::peripheral::Peripheral;
use esp_hal::peripherals::RMT;
use esp_hal::rmt::{
    Channel,
    Error,
    Event,
    Rmt,
    TxChannelConfig,
    TxChannelCreator,
    TxChannelInternal,
};
use esp_hal::time::Rate;
use esp_hal::{handler, Blocking};

type RmtChan = Channel<Blocking, 0>;

pub const RMT_RAM_START: usize = 0x60016400;
pub const RMT_CHANNEL_RAM_SIZE: usize = 48;
pub const USED_CHANNELS: usize = 4;
pub const HALF_USED_CHANNELS: usize = 2;

static WAKER: AtomicWaker = AtomicWaker::new();
static RMT_DATA: AtomicPtr<RmtData> = AtomicPtr::new(core::ptr::null_mut());

struct RmtData {
    handler: fn(&mut RmtData),
    iter: NonNull<()>,
    first_half: bool,
    status: EnumSet<Event>,
}

impl RmtData {
    fn calc_rmt_ram_addr(first_half: bool) -> *mut u32 {
        let ptr = RMT_RAM_START as *mut u32;
        if first_half {
            ptr
        } else {
            unsafe { ptr.add(RMT_CHANNEL_RAM_SIZE * HALF_USED_CHANNELS) }
        }
    }

    fn handle<T: FusedIterator<Item = u32>>(data: &mut RmtData) {
        let ptr = Self::calc_rmt_ram_addr(data.first_half);
        let iter = unsafe { data.iter.cast::<T>().as_mut() };

        let mut curr_iter = iter.take(RMT_CHANNEL_RAM_SIZE * HALF_USED_CHANNELS).enumerate();
        for (idx, entry) in &mut curr_iter {
            unsafe {
                ptr.add(idx).write_volatile(entry);
            }
        }

        data.first_half = !data.first_half;
    }

    unsafe fn get_inst(ptr: &AtomicPtr<RmtData>) -> Option<&'static mut RmtData> {
        let rmt_data_ptr = ptr.load(core::sync::atomic::Ordering::SeqCst);
        if !rmt_data_ptr.is_null() {
            let rmt_data = unsafe { &mut *rmt_data_ptr };
            Some(rmt_data)
        } else {
            None
        }
    }
}

#[handler]
fn rmt_interrupt_handle() {
    let st = RMT::regs().int_st().read();

    if st.ch0_tx_thr_event().bit() {
        RMT::regs()
            .int_clr()
            .write(|w| w.ch0_tx_thr_event().set_bit());

        if let Some(rmt_data) = unsafe { RmtData::get_inst(&RMT_DATA) } {
            (rmt_data.handler)(rmt_data);
        }
    }

    if st.ch0_tx_end().bit() || st.ch0_tx_err().bit() {
        if let Some(rmt_data) = unsafe { RmtData::get_inst(&RMT_DATA) } {
            let mut events: EnumSet<Event> = EnumSet::empty();
            if st.ch0_tx_end().bit() {
                events |= Event::End;
            }
            if st.ch0_tx_err().bit() {
                events |= Event::Error;
            }
            rmt_data.status = events;
        }

        RmtChan::clear_interrupts();
        // Tell [`RmtWaitDone::poll`] we're finished.
        RMT_DATA.store(core::ptr::null_mut(), core::sync::atomic::Ordering::SeqCst);
        WAKER.wake();
    }
}

/// A async RMT driver that allows sending iterators.
pub struct IterRmt {
    _rmt: RmtChan,
}

impl IterRmt {
    
    /// Create a new RMT that allows sending iterators.
    ///
    /// - `rmt_periph`: The RMT peripheral.
    /// - `tick_rate`: The frequency of ticks that is used as the base unit in [`esp_hal::rmt::PulseCode`].
    /// - `gpio`: The output GPIO where the waveform is output.
    /// - `tx_cfg`: The transmission configuration.
    pub fn new(
        rmt_periph: impl Peripheral<P = RMT>,
        tick_rate: Rate,
        gpio: impl Peripheral<P = impl PeripheralOutput>,
        tx_cfg: TxChannelConfig,
    ) -> Self {
        let mut rmt = Rmt::new(rmt_periph, tick_rate).unwrap();
        rmt.set_interrupt_handler(rmt_interrupt_handle);
        let rmt = rmt.channel0.configure(gpio, tx_cfg).unwrap();

        RmtChan::update();
        RmtChan::set_memsize(USED_CHANNELS as u8); // Use the RAM of all four channels.
        RmtChan::update();
        RmtChan::set_threshold((RMT_CHANNEL_RAM_SIZE * HALF_USED_CHANNELS) as u8);
        RmtChan::update();
        RmtChan::set_continuous(false);
        RmtChan::update();
        RmtChan::set_wrap_mode(true);

        Self { _rmt: rmt }
    }

    /// Transmit the given iterator asynchronously.
    pub async fn transmit<T>(&mut self, iter: T) -> Result<(), esp_hal::rmt::Error>
    where
        T: IntoIterator<Item = u32>,
        T::IntoIter: Sync + 'static,
    {
        let mut iter = iter.into_iter().chain(core::iter::once(0u32)).fuse();
        
        let mut rmt_data = RmtData {
            handler: RmtData::handle::<Fuse<Chain<T::IntoIter, Once<u32>>>>,
            iter: NonNull::from(&mut iter).cast::<()>(),
            first_half: true,
            status: EnumSet::empty()
        };
        
        (rmt_data.handler)(&mut rmt_data); // Fill first half.
        (rmt_data.handler)(&mut rmt_data); // Fill second half.

        RMT_DATA.store(&mut rmt_data as *mut _, core::sync::atomic::Ordering::SeqCst);

        RmtChan::clear_interrupts();
        RmtChan::listen_interrupt(Event::Error | Event::Threshold | Event::End);
        RmtChan::update();
        RmtChan::start_tx();

        RmtWaitDone.await;

        if rmt_data.status.contains(Event::Error) {
            Err(Error::TransmissionError)
        } else {
            Ok(())
        }
    }
}

struct RmtWaitDone;
impl Future for RmtWaitDone
{
    type Output = ();
    fn poll(
        self: core::pin::Pin<&mut Self>,
        cx: &mut core::task::Context<'_>,
    ) -> core::task::Poll<Self::Output> {
        WAKER.register(cx.waker());
        use core::task::Poll;
        if RMT_DATA.load(core::sync::atomic::Ordering::SeqCst).is_null() {
            Poll::Ready(())
        } else {
            Poll::Pending
        }
    }
}