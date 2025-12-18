use core::cell::UnsafeCell;
use core::future::poll_fn;
use core::marker::PhantomData;
use core::mem::ManuallyDrop;

use critical_section::{CriticalSection, Mutex};
use enumset::{EnumSet, EnumSetType};
use esp_hal::asynch::AtomicWaker;
use esp_hal::gpio::interconnect::PeripheralOutput;
use esp_hal::gpio::{self, Level, OutputConfig};
use esp_hal::peripherals::RMT;
use esp_hal::time::Rate;
use esp_hal::{handler, rmt};
use portable_atomic::{AtomicPtr, Ordering};
use portable_atomic_util::{Arc, Weak};

pub const RMT_RAM_START: usize = 0x60016400;
pub const RMT_CHANNEL_RAM_SIZE: usize = 48;
pub const NUM_TX_CHANNELS: usize = 2;

#[derive(Debug, EnumSetType)]
enum Event {
    Error,
    Threshold,
    End,
    LoopCount,
}

#[derive(Debug, Clone)]
pub struct TxChannelConfig {
    /// Channel's clock divider
    pub clk_divider: u8,
    /// Set the idle output level to low/high
    pub idle_output_level: Level,
    /// Enable idle output
    pub idle_output: bool,
    /// Enable carrier modulation
    pub carrier_modulation: bool,
    /// Carrier high phase in ticks
    pub carrier_high: u16,
    /// Carrier low phase in ticks
    pub carrier_low: u16,
    /// Level of the carrier
    pub carrier_level: Level,
    /// The amount of memory blocks allocated to this channel
    pub memsize: u8,
}

impl Default for TxChannelConfig {
    fn default() -> Self {
        Self {
            clk_divider: Default::default(),
            idle_output_level: Level::Low,
            idle_output: Default::default(),
            carrier_modulation: Default::default(),
            carrier_high: Default::default(),
            carrier_low: Default::default(),
            carrier_level: Level::Low,
            memsize: 1,
        }
    }
}

struct RmtChan(usize);

#[allow(dead_code)]
impl RmtChan {
    #[inline]
    pub fn ch_idx(&self) -> usize {
        self.0
    }

    pub fn set_divider(&self, divider: u8) {
        let rmt = RMT::regs();
        rmt.ch_tx_conf0(self.ch_idx())
            .modify(|_, w| unsafe { w.div_cnt().bits(divider) });
    }

    #[inline]
    pub fn update(&self) {
        let rmt = RMT::regs();
        rmt.ch_tx_conf0(self.ch_idx())
            .modify(|_, w| w.conf_update().set_bit());
    }

    pub fn set_memsize(&self, value: u8) {
        let rmt = RMT::regs();
        rmt.ch_tx_conf0(self.ch_idx())
            .modify(|_, w| unsafe { w.mem_size().bits(value) });
    }

    #[inline]
    pub fn memsize(&self) -> u8 {
        let rmt = RMT::regs();
        rmt.ch_tx_conf0(self.ch_idx()).read().mem_size().bits() as u8
    }

    #[inline]
    pub fn clear_tx_interrupts(&self) {
        let rmt = RMT::regs();

        rmt.int_clr().write(|w| {
            w.ch_tx_end(self.ch_idx() as u8).set_bit();
            w.ch_tx_err(self.ch_idx() as u8).set_bit();
            w.ch_tx_loop(self.ch_idx() as u8).set_bit();
            w.ch_tx_thr_event(self.ch_idx() as u8).set_bit()
        });
    }

    #[inline]
    pub fn set_tx_continuous(&self, continuous: bool) {
        let rmt = RMT::regs();

        rmt.ch_tx_conf0(self.ch_idx().into())
            .modify(|_, w| w.tx_conti_mode().bit(continuous));
    }

    #[inline]
    pub fn set_tx_wrap_mode(&self, wrap: bool) {
        let rmt = RMT::regs();

        rmt.ch_tx_conf0(self.ch_idx().into())
            .modify(|_, w| w.mem_tx_wrap_en().bit(wrap));
    }

    pub fn set_tx_carrier(&self, carrier: bool, high: u16, low: u16, level: Level) {
        let rmt = RMT::regs();

        rmt.chcarrier_duty(self.ch_idx().into())
            .write(|w| unsafe { w.carrier_high().bits(high).carrier_low().bits(low) });

        rmt.ch_tx_conf0(self.ch_idx().into()).modify(|_, w| {
            w.carrier_en().bit(carrier);
            w.carrier_eff_en().set_bit();
            w.carrier_out_lv().bit(level.into())
        });
    }

    pub fn set_tx_idle_output(&self, enable: bool, level: Level) {
        let rmt = RMT::regs();
        rmt.ch_tx_conf0(self.ch_idx().into())
            .modify(|_, w| w.idle_out_en().bit(enable).idle_out_lv().bit(level.into()));
    }

    #[inline]
    pub fn start_tx(&self) {
        let rmt = RMT::regs();

        rmt.ch_tx_conf0(self.ch_idx().into()).modify(|_, w| {
            w.mem_rd_rst().set_bit();
            w.apb_mem_rst().set_bit();
            w.tx_start().set_bit()
        });
    }

    // Return the first flag that is set of, in order of decreasing priority,
    // Event::Error, Event::End, Event::LoopCount, Event::Threshold
    #[inline]
    pub fn get_tx_status(&self) -> Option<Event> {
        let rmt = RMT::regs();
        let reg = rmt.int_raw().read();
        let ch = self.ch_idx() as u8;

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
    pub fn reset_tx_threshold_set(&self) {
        let rmt = RMT::regs();
        rmt.int_clr()
            .write(|w| w.ch_tx_thr_event(self.ch_idx() as u8).set_bit());
    }

    #[inline]
    pub fn set_tx_threshold(&self, threshold: u8) {
        let rmt = RMT::regs();
        rmt.ch_tx_lim(self.ch_idx().into())
            .modify(|_, w| unsafe { w.tx_lim().bits(threshold as u16) });
    }

    #[inline]
    pub fn is_tx_loopcount_interrupt_set(&self) -> bool {
        let rmt = RMT::regs();
        rmt.int_raw().read().ch_tx_loop(self.ch_idx() as u8).bit()
    }

    // Requires an update() call
    #[inline]
    pub fn stop_tx(&self) {
        let rmt = RMT::regs();
        rmt.ch_tx_conf0(self.ch_idx().into())
            .modify(|_, w| w.tx_stop().set_bit());
    }

    #[inline]
    pub fn set_tx_interrupt(&self, events: EnumSet<Event>, enable: bool) {
        let rmt = RMT::regs();
        rmt.int_ena().modify(|_, w| {
            if events.contains(Event::Error) {
                w.ch_tx_err(self.ch_idx() as u8).bit(enable);
            }
            if events.contains(Event::End) {
                w.ch_tx_end(self.ch_idx() as u8).bit(enable);
            }
            if events.contains(Event::Threshold) {
                w.ch_tx_thr_event(self.ch_idx() as u8).bit(enable);
            }
            w
        });
    }

    #[inline]
    fn listen_tx_interrupt(&self, event: impl Into<EnumSet<Event>>) {
        self.set_tx_interrupt(event.into(), true);
    }

    #[inline]
    fn unlisten_tx_interrupt(&self, event: impl Into<EnumSet<Event>>) {
        self.set_tx_interrupt(event.into(), false);
    }
}

static RMT_DATA: [AtomicPtr<RmtData>; NUM_TX_CHANNELS] =
    [const { AtomicPtr::new(core::ptr::null_mut()) }; NUM_TX_CHANNELS];

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
    handler: unsafe fn(usize, &mut RmtDataInner),
    iter: *mut (), // type erased iterator
    first_half: bool,
    half_size: usize,
    status: EnumSet<Event>,
}

unsafe impl Send for RmtDataInner {}

impl RmtData {
    unsafe fn try_get_inst(channel: usize) -> Option<Arc<RmtData>> {
        let ptr = RMT_DATA[channel].load(Ordering::SeqCst);
        if ptr.is_null() {
            return None;
        }

        // Do not drop the Weak, we want to preserve it for future `try_get_inst()` calls.
        ManuallyDrop::new(unsafe { Weak::from_raw(ptr) }).upgrade()
    }

    unsafe fn drop_weak(channel: usize) {
        let ptr = RMT_DATA[channel].swap(core::ptr::null_mut(), Ordering::SeqCst);
        if !ptr.is_null() {
            unsafe {
                Weak::from_raw(ptr);
            }
        }
    }

    unsafe fn stop(channel: usize) {
        critical_section::with(|_| {
            // RmtChan::stop();

            // Somehow stopping the transmission by setting the STOP flag
            // doesn't work (maybe a simulator problem?). Instead we fill
            // the beginning with zeros and start again, causing the transmission
            // to stop immediately.
            let ptr = Self::calc_rmt_ram_addr(channel);
            for (idx, entry) in core::iter::repeat_n(0_u32, 8).enumerate() {
                unsafe {
                    ptr.add(idx).write_volatile(entry);
                }
            }
            let chan = RmtChan(channel);
            chan.start_tx();
            chan.update();

            chan.unlisten_tx_interrupt(Event::Threshold);
            unsafe {
                RmtData::drop_weak(channel);
            }
        });
    }

    fn calc_rmt_ram_addr(channel: usize) -> *mut u32 {
        let ptr = RMT_RAM_START as *mut u32;
        unsafe { ptr.add(RMT_CHANNEL_RAM_SIZE * channel) }
    }

    unsafe fn handle<T: Iterator<Item = u32>>(channel: usize, data: &mut RmtDataInner) {
        let half_size = data.half_size;
        let first_half = data.first_half;

        let (start_ptr, half_ptr) = unsafe {
            let ptr = Self::calc_rmt_ram_addr(channel);
            (ptr, ptr.add(half_size))
        };
        let end_ptr = unsafe { half_ptr.add(half_size) };
        let mut ptr = if first_half { start_ptr } else { half_ptr };

        let iter_ptr = data.iter.cast::<T>();
        if let Some(iter) = unsafe { iter_ptr.as_mut() } {
            // Fill half of the RMT RAM with data from the iterator.
            for _ in 0..half_size {
                let value = match iter.next() {
                    Some(v) => v,
                    None => {
                        // The iterator is exhausted, mark this in the
                        // data so that we don't try to read from it again.
                        data.iter = core::ptr::null_mut();
                        break;
                    }
                };

                unsafe {
                    ptr.write_volatile(value);
                    ptr = ptr.add(1);
                }
            }
            // Wrap back to start if we reached the end.
            if ptr >= end_ptr {
                ptr = start_ptr;
            }
        }
        // Write a terminating zero if the iterator is exhausted.
        if data.iter.is_null() {
            unsafe {
                ptr.write_volatile(0_u32);
            }
        }

        // Toggle which half to fill next time.
        data.first_half = !first_half;
    }
}

#[handler]
fn rmt_interrupt_handle() {
    let st = RMT::regs().int_st().read();

    for ch_idx in 0..(NUM_TX_CHANNELS as u8) {
        if st.ch_tx_thr_event(ch_idx).bit() {
            RMT::regs()
                .int_clr()
                .write(|w| w.ch_tx_thr_event(ch_idx).set_bit());
            if let Some(rmt_data) = unsafe { RmtData::try_get_inst(ch_idx as usize) } {
                // Safety: We're in an interrupt and no other interrupt accesses this data.
                let token = unsafe { CriticalSection::new() };
                let inner = rmt_data.inner.borrow(token).get();

                unsafe {
                    let inner = (*inner).as_mut().unwrap_unchecked();
                    (inner.handler)(ch_idx as usize, inner);
                }
            }
        }

        if st.ch_tx_end(ch_idx).bit() || st.ch_tx_err(ch_idx).bit() {
            RmtChan(ch_idx as usize).clear_tx_interrupts();
            if let Some(rmt_data) = unsafe { RmtData::try_get_inst(ch_idx as usize) } {
                let mut events: EnumSet<Event> = EnumSet::empty();
                if st.ch_tx_end(ch_idx).bit() {
                    events |= Event::End;
                }
                if st.ch_tx_err(ch_idx).bit() {
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
                    RmtData::drop_weak(ch_idx as usize);
                }
            }
        }
    }
}

pub struct Rmt<'rmt> {
    _rmt: PhantomData<RMT<'rmt>>,
}

impl<'rmt> Rmt<'rmt> {
    /// Create a new RMT driver.
    ///
    /// - `rmt_periph`: The RMT peripheral.
    /// - `tick_rate`: The frequency of ticks that is used as the base unit in [`esp_hal::rmt::PulseCode`].
    pub fn new(rmt_periph: RMT<'rmt>, tick_rate: Rate) -> Self {
        let mut rmt = rmt::Rmt::new(rmt_periph, tick_rate).unwrap();
        rmt.set_interrupt_handler(rmt_interrupt_handle);

        // TODO: Is this needed?
        // Prevent a possible drop implementation within the esp-hal implementation from
        // running, which could uninitialize.
        core::mem::forget(rmt);
        Self { _rmt: PhantomData }
    }

    /// Create a new RMT channel that can send iterators.
    ///
    /// - `ch_idx`: The channel index (0 or 1).
    /// - `gpio`: The output GPIO where the waveform is output.
    /// - `tx_cfg`: The transmission configuration.
    pub fn channel(
        &'_ mut self,
        ch_idx: usize,
        gpio: impl PeripheralOutput<'rmt>,
        tx_cfg: TxChannelConfig,
    ) -> IterRmtChannel<'rmt> {
        IterRmtChannel::new(ch_idx, gpio.into(), tx_cfg)
    }
}

/// A async RMT driver that allows sending iterators.
pub struct IterRmtChannel<'ch> {
    _rmt: PhantomData<Rmt<'ch>>,
    channel: RmtChan,
    data: Arc<RmtData>,
}

impl<'rmt> IterRmtChannel<'rmt> {
    /// Create a new RMT that allows sending iterators.
    ///
    /// - `ch_idx`: The channel index (0 or 1).
    /// - `gpio`: The output GPIO where the waveform is output.
    /// - `tx_cfg`: The transmission configuration.
    fn new(
        ch_idx: usize,
        gpio: gpio::interconnect::OutputSignal<'rmt>,
        tx_cfg: TxChannelConfig,
    ) -> IterRmtChannel<'rmt> {
        let channel = RmtChan(ch_idx);

        gpio.apply_output_config(&OutputConfig::default());
        gpio.set_output_enable(true);

        match ch_idx {
            0 => gpio::OutputSignal::RMT_SIG_0,
            1 => gpio::OutputSignal::RMT_SIG_1,
            _ => panic!("invalid RMT channel"),
        }
        .connect_to(&gpio);
        
        channel.update();
        channel.set_divider(tx_cfg.clk_divider);
        channel.set_tx_carrier(
            tx_cfg.carrier_modulation,
            tx_cfg.carrier_high,
            tx_cfg.carrier_low,
            tx_cfg.carrier_level,
        );
        channel.set_memsize(tx_cfg.memsize);
        channel.set_tx_idle_output(tx_cfg.idle_output, tx_cfg.idle_output_level);
        channel.set_tx_threshold(((RMT_CHANNEL_RAM_SIZE * tx_cfg.memsize as usize) / 2) as u8);
        channel.set_tx_continuous(false);
        channel.set_tx_wrap_mode(true);
        channel.update();

        Self {
            _rmt: PhantomData,
            channel,
            data: Arc::default(),
        }
    }

    /// Transmit the given iterator asynchronously.
    ///
    /// - `iter`: An iterator that yields `u32` values representing RMT pulse codes.
    /// 
    /// Returns the iterator when transmission is complete.
    /// 
    /// Awaiting this function will yield until the transmission is finished.
    /// When this future is dropped before completion, the transmission is stopped.
    pub async fn transmit<T>(&mut self, iter: T) -> Result<T::IntoIter, Error>
    where
        T: IntoIterator<Item = u32>,
        T::IntoIter: Sync + 'static,
    {
        struct Canceller(u8);
        impl Drop for Canceller {
            fn drop(&mut self) {
                unsafe {
                    RmtData::stop(self.0 as usize);
                }
            }
        }

        let ch_idx = self.channel.ch_idx();
        let canceller = Canceller(ch_idx as u8);
        let mut iter = iter.into_iter();

        let mut data_inner = RmtDataInner {
            handler: RmtData::handle::<T::IntoIter>,
            iter: &mut iter as *mut _ as *mut (), // cast to type erased pointer
            first_half: true,
            half_size: self.channel.memsize() as usize * RMT_CHANNEL_RAM_SIZE / 2,
            status: EnumSet::empty(),
        };

        self.channel.clear_tx_interrupts();
        self.channel
            .listen_tx_interrupt(Event::Error | Event::Threshold | Event::End);

        // Prefill both halves of the RMT RAM.
        unsafe {
            (data_inner.handler)(ch_idx, &mut data_inner); // Fill first half.
            (data_inner.handler)(ch_idx, &mut data_inner); // Fill second half.
        }

        critical_section::with(|_| {
            let data = Arc::get_mut(&mut self.data)
                .unwrap()
                .inner
                .get_mut()
                .get_mut();
            *data = Some(data_inner);

            RMT_DATA[ch_idx].store(
                Arc::downgrade(&self.data).into_raw() as *mut _,
                Ordering::SeqCst,
            );
        });

        self.channel.start_tx();
        self.channel.update();

        // Wait for transmission to finish.
        poll_fn(move |cx| {
            use core::task::Poll;
            if let Some(rmt_data) = unsafe { RmtData::try_get_inst(ch_idx) } {
                rmt_data.waker.register(cx.waker());
                Poll::Pending
            } else {
                Poll::Ready(())
            }
        })
        .await;

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
