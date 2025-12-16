use core::cell::Cell;

use embassy_executor::Spawner;
use esp_hal::gpio::{Input, Level};
use esp_println::dbg;
use futures::future::select;
use futures::{FutureExt, select_biased};

use crate::cmd::{StepperCmd, StepperCmdSignal, StopCmd, StopCmdSub};
use crate::stepper::{self, Dir, Stepper};
use crate::utils::{Mutex, Signal};

#[derive(Debug)]
struct HomingState {
    homing_needed: bool,
    end_pos: i32,
}

impl HomingState {
    async fn home_stepper(
        &mut self,
        stepper: &mut Stepper<'static>,
        end_switch_sig: &'static Signal<()>,
        emergency_stop_sig: &'static Signal<()>,
    ) {
        if end_switch_sig.signaled() {
            log::error!("Homing failed: end switch already active\r");
        }
        stepper.set_curr_pos(0);
        let mut res =
            move_to_end_switch(Dir::Negative, stepper, emergency_stop_sig, end_switch_sig).await;
        if !res {
            log::error!("Homing failed: could not reach end switch\r");
            return;
        }
        res = move_away_from_end_switch(Dir::Negative, stepper, emergency_stop_sig).await;
        if !res {
            log::error!("Homing failed: could not move away from end switch\r");
            return;
        }
        log::info!(
            "Homing to start position successful ({} steps)\r",
            stepper.curr_pos().abs() / 8
        );
        stepper.set_curr_pos(0);

        let mut res =
            move_to_end_switch(Dir::Positive, stepper, emergency_stop_sig, end_switch_sig).await;
        if !res {
            log::error!("Homing failed: could not reach end switch\r");
            return;
        }
        res = move_away_from_end_switch(Dir::Positive, stepper, emergency_stop_sig).await;
        if !res {
            log::error!("Homing failed: could not move away from end switch\r");
            return;
        }
        self.end_pos = stepper.curr_pos();
        self.homing_needed = false;

        log::info!("Homing successful, end pos = {}\r", self.end_pos / 8);
    }
}

static HOMING_MOVE_TARGET_LEVEL: Mutex<Cell<Option<Level>>> = Mutex::new(Cell::new(None));
#[embassy_executor::task]
async fn route_end_switch_trigger(
    mut end_switch: Input<'static>,
    stepper_stop_sig: &'static stepper::StopSignal,
    end_switch_sig: &'static Signal<()>,
    homing_move_target_level: &'static Mutex<Cell<Option<Level>>>,
) {
    loop {
        // TODO: Debounce?
        end_switch.wait_for_any_edge().await;
        let level = end_switch.level();
        if level == Level::Low {
            end_switch_sig.signal(());
        }

        // If we are doing a homing move we want to gracefully stop when the end switch
        // has the set target level.
        let maybe_target_level = homing_move_target_level.lock(|c| c.get());
        match (maybe_target_level, level) {
            (Some(Level::High), Level::High) | (Some(Level::Low), Level::Low) => {
                stepper_stop_sig.signal(());
            }
            _ => (),
        }

        log::info!("End switch triggered\r");
    }
}

/// Route stop subscription to stepper stop signals.
#[embassy_executor::task]
async fn stepper_route_stop(
    mut input_stop: StopCmdSub,
    stepper_stop_sig: &'static stepper::StopSignal,
    emergency_stop_sig: &'static Signal<()>,
) {
    loop {
        let stop_cmd = input_stop.next_message_pure().await;
        match stop_cmd {
            StopCmd::Immediate => emergency_stop_sig.signal(()),
            StopCmd::Graceful => stepper_stop_sig.signal(()),
            StopCmd::Continue => {
                emergency_stop_sig.reset();
                stepper_stop_sig.reset();
            }
        }
    }
}

#[embassy_executor::task]
pub async fn stepper_task(
    mut stepper: Stepper<'static>,
    end_switch: Input<'static>,
    stepper_cmd_sig: &'static StepperCmdSignal,
    stop_cmd_sub: StopCmdSub,
    spawner: Spawner,
) {
    static STOP_SIGNAL: stepper::StopSignal = stepper::StopSignal::new();
    static EMERGENCY_STOP_SIGNAL: Signal<()> = Signal::new();
    static END_SWITCH_SIGNAL: Signal<()> = Signal::new();

    spawner.must_spawn(stepper_route_stop(
        stop_cmd_sub,
        &STOP_SIGNAL,
        &EMERGENCY_STOP_SIGNAL,
    ));
    spawner.must_spawn(route_end_switch_trigger(
        end_switch,
        &STOP_SIGNAL,
        &END_SWITCH_SIGNAL,
        &HOMING_MOVE_TARGET_LEVEL,
    ));
    stepper.set_stop_signal(&STOP_SIGNAL);

    let mut homing_state = HomingState {
        homing_needed: true,
        end_pos: 0,
    };

    loop {
        let cmd = stepper_cmd_sig.receive().await;
        let pos = match cmd.value {
            StepperCmd::GoTo(pos) => {
                if !homing_state.homing_needed {
                    pos
                } else {
                    log::warn!("Ignoring command, homing required\r");
                    continue;
                }
            }
            StepperCmd::Home() => {
                homing_state
                    .home_stepper(&mut stepper, &END_SWITCH_SIGNAL, &EMERGENCY_STOP_SIGNAL)
                    .await;
                continue;
            }
        };

        assert!(
            !homing_state.homing_needed,
            "Homing should have been handled above"
        );
        select_biased! {
            _ = EMERGENCY_STOP_SIGNAL.wait().fuse() => {
                homing_state.homing_needed = true;
                continue;
            },
            _ = END_SWITCH_SIGNAL.wait().fuse() => {
                let prev_dir = stepper.last_dir().unwrap();
                log::warn!("End switch triggered ({prev_dir:?})\r");
                let res = move_away_from_end_switch(prev_dir, &mut stepper, &EMERGENCY_STOP_SIGNAL).await;
                if !res {
                    homing_state.homing_needed = true;
                    continue;
                }
                match prev_dir {
                    // We are at the end position when hitting the end switch in positive direction.
                    Dir::Positive => {
                        stepper.set_curr_pos(homing_state.end_pos);
                    },
                    // We are at position 0 when hitting the end switch in negative direction.
                    Dir::Negative => {
                        stepper.set_curr_pos(0);
                    }
                }
            },
            _ = stepper.run_to_pos(pos).fuse() => ()
        };
    }
}

async fn move_away_from_end_switch(
    prev_dir: Dir,
    stepper: &mut Stepper<'static>,
    emergency_stop_sig: &'static Signal<()>,
) -> bool {
    const STEPS: u32 = 10000000;
    let new_dir = prev_dir.opposite();

    HOMING_MOVE_TARGET_LEVEL.lock(|c| c.set(Some(Level::High)));

    log::info!("Moving away from end switch (dir = {new_dir:?})\r");
    let res = select_biased! {
        _ = emergency_stop_sig.wait().fuse() => {
            false
        },
        steps = stepper
        .run_steps_with_accel_speed(STEPS, new_dir, crate::STEPPER_HOMING_ACCEL_SPEED)
        .fuse() => {
            stepper.offset_curr_pos(new_dir.for_steps(steps));
            true
        }
    };

    HOMING_MOVE_TARGET_LEVEL.lock(|c| c.set(None));
    res
}

async fn move_to_end_switch(
    dir: Dir,
    stepper: &mut Stepper<'static>,
    emergency_stop_sig: &'static Signal<()>,
    end_switch_sig: &'static Signal<()>,
) -> bool {
    const STEPS: u32 = 10000000;

    HOMING_MOVE_TARGET_LEVEL.lock(|c| c.set(Some(Level::Low)));

    log::info!("Moving to end switch (dir = {dir:?})\r");
    let res = select_biased! {
        _ = emergency_stop_sig.wait().fuse() => {
            false
        },
        steps = stepper
        .run_steps_with_accel_speed(STEPS, dir, crate::STEPPER_HOMING_ACCEL_SPEED)
        .fuse() => {
            stepper.offset_curr_pos(dir.for_steps(steps));
            true
        }
    };
    HOMING_MOVE_TARGET_LEVEL.lock(|c| c.set(None));
    // Moving to the end switch triggers the end switch signal, so we reset it here.
    end_switch_sig.reset();
    res
}
