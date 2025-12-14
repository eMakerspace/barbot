use core::cell::UnsafeCell;
use core::future::Future;
use core::iter::{Chain, Fuse, FusedIterator, Once};
use core::mem::ManuallyDrop;
use core::ptr::NonNull;

use critical_section::{CriticalSection, Mutex};
use enumset::{EnumSet, EnumSetType};
use esp_hal::asynch::AtomicWaker;
use esp_hal::gpio::interconnect::PeripheralOutput;
use esp_hal::gpio::Level;
use esp_hal::peripherals::RMT;
use esp_hal::rmt::{Channel, Rmt, TxChannelConfig, TxChannelCreator};
use esp_hal::time::Rate;
use esp_hal::{handler, Blocking};
use portable_atomic::{AtomicPtr, Ordering};
use portable_atomic_util::{Arc, Weak};

pub const RMT_RAM_START: usize = 0x60016400;
pub const RMT_CHANNEL_RAM_SIZE: usize = 48;
pub const USED_CHANNELS: usize = 4;
pub const HALF_USED_CHANNELS: usize = 2;

#[derive(Debug, EnumSetType)]
enum Event {
    Error,
    Threshold,
    End,
    LoopCount,
}

struct RmtChan;

#[allow(dead_code)]
impl RmtChan {
    const CH_IDX: usize = 0;

    #[inline]
    pub fn update() {
        let rmt = RMT::regs();
        rmt.ch_tx_conf0(Self::CH_IDX)
            .modify(|_, w| w.conf_update().set_bit());
    }

    pub fn set_memsize(value: u8) {
        let rmt = RMT::regs();
        rmt.ch_tx_conf0(Self::CH_IDX)
            .modify(|_, w| unsafe { w.mem_size().bits(value) });
    }

    #[inline]
    pub fn clear_tx_interrupts() {
        let rmt = RMT::regs();

        rmt.int_clr().write(|w| {
            w.ch_tx_end(Self::CH_IDX as u8).set_bit();
            w.ch_tx_err(Self::CH_IDX as u8).set_bit();
            w.ch_tx_loop(Self::CH_IDX as u8).set_bit();
            w.ch_tx_thr_event(Self::CH_IDX as u8).set_bit()
        });
    }

    #[inline]
    pub fn set_tx_continuous(continuous: bool) {
        let rmt = RMT::regs();

        rmt.ch_tx_conf0(Self::CH_IDX.into())
            .modify(|_, w| w.tx_conti_mode().bit(continuous));
    }

    #[inline]
    pub fn set_tx_wrap_mode(wrap: bool) {
        let rmt = RMT::regs();

        rmt.ch_tx_conf0(Self::CH_IDX.into())
            .modify(|_, w| w.mem_tx_wrap_en().bit(wrap));
    }

    pub fn set_tx_carrier(carrier: bool, high: u16, low: u16, level: Level) {
        let rmt = RMT::regs();

        rmt.chcarrier_duty(Self::CH_IDX.into())
            .write(|w| unsafe { w.carrier_high().bits(high).carrier_low().bits(low) });

        rmt.ch_tx_conf0(Self::CH_IDX.into()).modify(|_, w| {
            w.carrier_en().bit(carrier);
            w.carrier_eff_en().set_bit();
            w.carrier_out_lv().bit(level.into())
        });
    }

    pub fn set_tx_idle_output(enable: bool, level: Level) {
        let rmt = RMT::regs();
        rmt.ch_tx_conf0(Self::CH_IDX.into())
            .modify(|_, w| w.idle_out_en().bit(enable).idle_out_lv().bit(level.into()));
    }

    #[inline]
    pub fn start_tx() {
        let rmt = RMT::regs();

        rmt.ch_tx_conf0(Self::CH_IDX.into()).modify(|_, w| {
            w.mem_rd_rst().set_bit();
            w.apb_mem_rst().set_bit();
            w.tx_start().set_bit()
        });
    }

    // Return the first flag that is set of, in order of decreasing priority,
    // Event::Error, Event::End, Event::LoopCount, Event::Threshold
    #[inline]
    pub fn get_tx_status() -> Option<Event> {
        let rmt = RMT::regs();
        let reg = rmt.int_raw().read();
        let ch = Self::CH_IDX as u8;

        if reg.ch_tx_end(ch).bit() {
            Some(Event::End)
        } else if reg.ch_tx_err(ch).bit() {
            Some(Event::Error)
        } else if reg.ch_tx_loop(ch).bit() {
            Some(Event::LoopCount)
        } else if reg.ch_tx_thr_event(ch).bit() {
            Some(Event::Threshold)
        } else {
            None
        }
    }

    #[inline]
    pub fn reset_tx_threshold_set() {
        let rmt = RMT::regs();
        rmt.int_clr()
            .write(|w| w.ch_tx_thr_event(Self::CH_IDX as u8).set_bit());
    }

    #[inline]
    pub fn set_tx_threshold(threshold: u8) {
        let rmt = RMT::regs();
        rmt.ch_tx_lim(Self::CH_IDX.into())
            .modify(|_, w| unsafe { w.tx_lim().bits(threshold as u16) });
    }

    #[inline]
    pub fn is_tx_loopcount_interrupt_set() -> bool {
        let rmt = RMT::regs();
        rmt.int_raw().read().ch_tx_loop(Self::CH_IDX as u8).bit()
    }

    // Returns whether stopping was immediate, or needs to wait for tx end.
    // Due to inlining, the compiler should be able to eliminate code in the caller that
    // depends on this.
    //
    // Requires an update() call
    #[inline]
    pub fn stop_tx(&self) -> bool {
        let rmt = RMT::regs();
        rmt.ch_tx_conf0(Self::CH_IDX.into())
            .modify(|_, w| w.tx_stop().set_bit());
        true
    }

    #[inline]
    pub fn set_tx_interrupt(events: EnumSet<Event>, enable: bool) {
        let rmt = RMT::regs();
        rmt.int_ena().modify(|_, w| {
            if events.contains(Event::Error) {
                w.ch_tx_err(Self::CH_IDX as u8).bit(enable);
            }
            if events.contains(Event::End) {
                w.ch_tx_end(Self::CH_IDX as u8).bit(enable);
            }
            if events.contains(Event::Threshold) {
                w.ch_tx_thr_event(Self::CH_IDX as u8).bit(enable);
            }
            w
        });
    }

    #[inline]
    fn listen_tx_interrupt(event: impl Into<EnumSet<Event>>) {
        Self::set_tx_interrupt(event.into(), true);
    }

    #[inline]
    fn unlisten_tx_interrupt(event: impl Into<EnumSet<Event>>) {
        Self::set_tx_interrupt(event.into(), false);
    }
}

static RMT_DATA: AtomicPtr<RmtData> = AtomicPtr::new(core::ptr::null_mut());

struct RmtData {
    waker: AtomicWaker,
    inner: Mutex<UnsafeCell<Option<RmtDataInner>>>,
}

impl Default for RmtData {
    fn default() -> Self {
        Self {
            waker: AtomicWaker::new(),
            inner: Mutex::new(UnsafeCell::new(None)),
        }
    }
}

struct RmtDataInner {
    handler: fn(&mut RmtDataInner),
    iter: NonNull<()>,
    first_half: bool,
    status: EnumSet<Event>,
}

unsafe impl Send for RmtDataInner {}

impl RmtData {
    unsafe fn try_get_inst() -> Option<Arc<RmtData>> {
        let ptr = RMT_DATA.load(Ordering::SeqCst);
        if ptr.is_null() {
            return None;
        }

        // Do not drop the Weak, we want to preserve it for future `try_get_inst()` calls.
        ManuallyDrop::new(unsafe { Weak::from_raw(ptr) }).upgrade()
    }

    unsafe fn drop_weak() {
        let ptr = RMT_DATA.swap(core::ptr::null_mut(), Ordering::SeqCst);
        if !ptr.is_null() {
            unsafe {
                Weak::from_raw(ptr);
            }
        }
    }

    fn stop() {
        critical_section::with(|_| {
            // RmtChan::stop();

            // Somehow stopping the transmission by setting the STOP flag
            // doesn't work (maybe a simulator problem?). Instead we fill
            // the beginning with zeros and start again, causing the transmission
            // to stop immediately.
            let ptr = Self::calc_rmt_ram_addr(true);
            for (idx, entry) in core::iter::repeat_n(0_u32, 8).enumerate() {
                unsafe {
                    ptr.add(idx).write_volatile(entry);
                }
            }
            RmtChan::start_tx();
            RmtChan::update();

            RmtChan::unlisten_tx_interrupt(Event::Threshold);
            unsafe {
                RmtData::drop_weak();
            }
        });
    }

    fn calc_rmt_ram_addr(first_half: bool) -> *mut u32 {
        let ptr = RMT_RAM_START as *mut u32;
        if first_half {
            ptr
        } else {
            unsafe { ptr.add(RMT_CHANNEL_RAM_SIZE * HALF_USED_CHANNELS) }
        }
    }

    fn handle<T: FusedIterator<Item = u32>>(data: &mut RmtDataInner) {
        let ptr = Self::calc_rmt_ram_addr(data.first_half);
        let mut iter = unsafe { data.iter.cast::<T>().as_mut() }.enumerate();

        const COUNT: usize = RMT_CHANNEL_RAM_SIZE * HALF_USED_CHANNELS;
        for (idx, entry) in (&mut iter).take(COUNT) {
            unsafe {
                ptr.add(idx).write_volatile(entry);
            }
        }
        data.first_half = !data.first_half;
    }
}

#[handler]
fn rmt_interrupt_handle() {
    let st = RMT::regs().int_st().read();

    if st.ch0_tx_thr_event().bit() {
        RMT::regs()
            .int_clr()
            .write(|w| w.ch0_tx_thr_event().set_bit());

        let Some(rmt_data) = (unsafe { RmtData::try_get_inst() }) else {
            return;
        };

        // Safety: We're in an interrupt and no other interrupt accesses this data.
        let token = unsafe { CriticalSection::new() };
        let inner = rmt_data.inner.borrow(token).get();

        unsafe {
            let inner = (*inner).as_mut().unwrap_unchecked();
            (inner.handler)(inner);
        }
    }

    if st.ch0_tx_end().bit() || st.ch0_tx_err().bit() {
        RmtChan::clear_tx_interrupts();
        let Some(rmt_data) = (unsafe { RmtData::try_get_inst() }) else {
            return;
        };
        let mut events: EnumSet<Event> = EnumSet::empty();
        if st.ch0_tx_end().bit() {
            events |= Event::End;
        }
        if st.ch0_tx_err().bit() {
            events |= Event::Error;
        }
        unsafe {
            (*rmt_data.inner.borrow(CriticalSection::new()).get())
                .as_mut()
                .unwrap_unchecked()
                .status |= events;
        }

        rmt_data.waker.wake();
        unsafe {
            RmtData::drop_weak();
        }
    }
}

/// A async RMT driver that allows sending iterators.
pub struct IterRmt<'ch> {
    _rmt: Channel<'ch, Blocking, esp_hal::rmt::Tx>,
    data: Arc<RmtData>,
}

impl<'rmt> IterRmt<'rmt> {
    /// Create a new RMT that allows sending iterators.
    ///
    /// - `rmt_periph`: The RMT peripheral.
    /// - `tick_rate`: The frequency of ticks that is used as the base unit in [`esp_hal::rmt::PulseCode`].
    /// - `gpio`: The output GPIO where the waveform is output.
    /// - `tx_cfg`: The transmission configuration.
    pub fn new(
        rmt_periph: RMT<'rmt>,
        tick_rate: Rate,
        gpio: impl PeripheralOutput<'rmt>,
        tx_cfg: TxChannelConfig,
    ) -> IterRmt<'rmt> {
        let mut rmt = Rmt::new(rmt_periph, tick_rate).unwrap();
        rmt.set_interrupt_handler(rmt_interrupt_handle);
        let rmt = rmt.channel0.configure_tx(gpio, tx_cfg).unwrap();

        RmtChan::set_memsize(USED_CHANNELS as u8); // Use the RAM of all four channels.
        RmtChan::set_tx_threshold((RMT_CHANNEL_RAM_SIZE * HALF_USED_CHANNELS) as u8);
        RmtChan::set_tx_continuous(false);
        RmtChan::set_tx_wrap_mode(true);
        RmtChan::update();

        Self {
            _rmt: rmt,
            data: Arc::default(),
        }
    }

    /// Transmit the given iterator asynchronously.
    pub async fn transmit<T>(&mut self, iter: T) -> Result<T::IntoIter, Error>
    where
        T: IntoIterator<Item = u32>,
        T::IntoIter: Sync + 'static,
    {
        struct Canceller;
        impl Drop for Canceller {
            fn drop(&mut self) {
                RmtData::stop();
            }
        }

        let canceller = Canceller;
        let mut iter = iter.into_iter();

        let mut iter_ext = (&mut iter).chain(core::iter::once(0u32)).fuse();

        let mut data_inner = RmtDataInner {
            handler: RmtData::handle::<Fuse<Chain<&mut T::IntoIter, Once<u32>>>>,
            iter: NonNull::from(&mut iter_ext).cast::<()>(),
            first_half: true,
            status: EnumSet::empty(),
        };

        RmtChan::clear_tx_interrupts();
        RmtChan::listen_tx_interrupt(Event::Error | Event::Threshold | Event::End);

        (data_inner.handler)(&mut data_inner); // Fill first half.
        (data_inner.handler)(&mut data_inner); // Fill second half.

        critical_section::with(|_| {
            let data = Arc::get_mut(&mut self.data)
                .unwrap()
                .inner
                .get_mut()
                .get_mut();
            *data = Some(data_inner);

            RMT_DATA.store(
                Arc::downgrade(&self.data).into_raw() as *mut _,
                Ordering::SeqCst,
            );
        });

        RmtChan::start_tx();
        RmtChan::update();

        RmtWaitDone.await;

        // The transaction is already finished, no need to stop it anymore.
        core::mem::forget(canceller);

        let rmt_data = critical_section::with(|_| Arc::get_mut(&mut self.data).unwrap());
        let status = rmt_data.inner.get_mut().get_mut().take().unwrap().status;

        if status.contains(Event::Error) {
            Err(Error::TransmissionError)
        } else if status.contains(Event::End) {
            Ok(iter)
        } else {
            Err(Error::Stopped)
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    TransmissionError,
    Stopped,
}

struct RmtWaitDone;
impl Future for RmtWaitDone {
    type Output = ();
    fn poll(
        self: core::pin::Pin<&mut Self>,
        cx: &mut core::task::Context<'_>,
    ) -> core::task::Poll<Self::Output> {
        use core::task::Poll;
        if let Some(rmt_data) = unsafe { RmtData::try_get_inst() } {
            rmt_data.waker.register(cx.waker());
            Poll::Pending
        } else {
            Poll::Ready(())
        }
    }
}
