use embassy_sync::blocking_mutex::raw::CriticalSectionRawMutex;

pub mod bi_signal {
    use core::cell::Cell;
    use core::fmt::Debug;
    use core::future::{Future, poll_fn};
    use core::task::{Context, Poll};

    use embassy_sync::blocking_mutex::Mutex;
    use embassy_sync::blocking_mutex::raw::{CriticalSectionRawMutex, RawMutex};

    #[derive(Debug)]
    enum State<T> {
        None,
        ReceiverWaiting {
            receiver_waker: core::task::Waker,
        },
        Sent {
            value: T,
            sender_waker: core::task::Waker,
        },
        SenderWaiting {
            sender_waker: core::task::Waker,
        },
    }

    pub struct BiSignal<T, M = CriticalSectionRawMutex>
    where
        M: RawMutex,
    {
        state: Mutex<M, Cell<State<T>>>,
    }

    impl<T, M> Debug for BiSignal<T, M>
    where
        M: RawMutex,
        T: Debug,
    {
        fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
            let mut s = f.debug_struct("BiSignal");
            self.state.lock(|cell| {
                let state = cell.replace(State::None);
                s.field("state", &state);
                cell.set(state);
            });
            s.finish()
        }
    }

    pub struct BiSignalSendFuture<'a, T, M>
    where
        M: RawMutex,
    {
        signal: &'a BiSignal<T, M>,
        value: Option<T>,
    }

    impl<T, M> Future for BiSignalSendFuture<'_, T, M>
    where
        M: RawMutex,
        T: Unpin,
    {
        type Output = ();

        fn poll(self: core::pin::Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
            let this = self.get_mut();
            let BiSignalSendFuture { signal, value } = this;
            signal.state.lock(|cell| {
                if let Some(value) = value.take() {
                    let state = cell.replace(State::Sent {
                        value,
                        sender_waker: cx.waker().clone(),
                    });

                    match state {
                        // Wake the receiver if it's waiting.
                        State::ReceiverWaiting { receiver_waker } => {
                            receiver_waker.wake();
                        }

                        // Wake a previous sender that is waiting.
                        State::Sent {
                            value: _,
                            sender_waker,
                        }
                        | State::SenderWaiting { sender_waker } => {
                            sender_waker.wake();
                        }
                        _ => (),
                    }
                    Poll::Pending
                } else {
                    let state = cell.replace(State::None);

                    let result = match &state {
                        // The receiver consumed the value, we are done.
                        State::None | State::ReceiverWaiting { .. } => Poll::Ready(()),
                        // A sender is waiting but it's not us, we are done.
                        State::Sent {
                            value: _,
                            sender_waker,
                        }
                        | State::SenderWaiting { sender_waker }
                            if !sender_waker.will_wake(cx.waker()) =>
                        {
                            Poll::Ready(())
                        }
                        // Otherwise, wait for the reciever to consume the value.
                        _ => Poll::Pending,
                    };
                    cell.set(state);
                    result
                }
            })
        }
    }

    pub struct BiSignalValue<'sig, T, M: RawMutex> {
        signal: &'sig BiSignal<T, M>,
        pub value: T,
    }

    impl<T, M> core::ops::Deref for BiSignalValue<'_, T, M>
    where
        M: RawMutex,
    {
        type Target = T;

        fn deref(&self) -> &Self::Target {
            &self.value
        }
    }

    impl<T, M> core::ops::DerefMut for BiSignalValue<'_, T, M>
    where
        M: RawMutex,
    {
        fn deref_mut(&mut self) -> &mut Self::Target {
            &mut self.value
        }
    }

    impl<T, M> Drop for BiSignalValue<'_, T, M>
    where
        M: RawMutex,
    {
        fn drop(&mut self) {
            self.signal.wake_sender();
        }
    }

    impl<T, M> BiSignalValue<'_, T, M>
    where
        M: RawMutex,
    {
        pub fn into_inner(self) -> T {
            self.signal.wake_sender();
            let this = core::mem::ManuallyDrop::new(self);
            // Safety: No drop is called on `this` so we can safely move out the value.
            unsafe { core::ptr::read(&this.value) }
        }
    }

    impl<T, M> BiSignal<T, M>
    where
        M: RawMutex,
    {
        pub const fn new() -> Self {
            Self {
                state: Mutex::new(Cell::new(State::None)),
            }
        }

        /// Send a value to the BiSignal and wait for it to be consumed by the receiver.
        pub fn send(&self, value: T) -> impl Future<Output = ()> + '_
        where
            T: Unpin,
        {
            BiSignalSendFuture {
                signal: self,
                value: Some(value),
            }
        }

        fn poll_receive(&self, cx: &mut Context<'_>) -> Poll<BiSignalValue<'_, T, M>> {
            self.state.lock(|cell| {
                let state = cell.replace(State::None);
                match state {
                    State::None => {
                        cell.set(State::ReceiverWaiting {
                            receiver_waker: cx.waker().clone(),
                        });
                        Poll::Pending
                    }
                    State::ReceiverWaiting { receiver_waker }
                        if receiver_waker.will_wake(cx.waker()) =>
                    {
                        cell.set(State::ReceiverWaiting { receiver_waker });
                        Poll::Pending
                    }
                    State::ReceiverWaiting { receiver_waker } => {
                        cell.set(State::ReceiverWaiting {
                            receiver_waker: cx.waker().clone(),
                        });
                        receiver_waker.wake();
                        Poll::Pending
                    }
                    State::Sent {
                        value,
                        sender_waker,
                    } => {
                        cell.set(State::SenderWaiting { sender_waker });
                        Poll::Ready(BiSignalValue {
                            signal: self,
                            value,
                        })
                    }
                    State::SenderWaiting { sender_waker } => {
                        // We called receive before comitting the previously received
                        // value (thus the sender is still waiting). Wait forever.
                        //
                        // This is useful if the receiver is selecting on multiple
                        // futures, and so actually wont wait forever.
                        cell.set(State::SenderWaiting { sender_waker });
                        Poll::Pending
                    }
                }
            })
        }

        /// Receive a value from the BiSignal.
        ///
        /// The sender is not notified until the returned BiSignalValue is dropped or consumed.
        pub fn receive(&self) -> impl Future<Output = BiSignalValue<'_, T, M>> {
            poll_fn(move |cx| self.poll_receive(cx))
        }

        fn wake_sender(&self) {
            self.state.lock(|cell| {
                let state = cell.replace(State::None);
                match state {
                    // Wake the sender if it's waiting.
                    State::SenderWaiting { sender_waker } => {
                        sender_waker.wake();
                    }
                    _ => {
                        cell.set(state);
                    }
                }
            })
        }
    }
}

pub use bi_signal::{BiSignal, BiSignalValue};
use esp_hal::gpio::Level;
pub type Mutex<T> = embassy_sync::blocking_mutex::Mutex<CriticalSectionRawMutex, T>;
pub type Signal<T> = embassy_sync::signal::Signal<CriticalSectionRawMutex, T>;

pub const fn invert_level(level: Level) -> Level {
    match level {
        Level::High => Level::Low,
        Level::Low => Level::High,
    }
}
