use futures::{FutureExt, select_biased};

use crate::cmd::{Cmd, CmdChannel, PumpCmdSignal, StepperCmdSignal, StopCmdSub};

pub struct HandleCmd {
    pub cmd_chan: &'static CmdChannel,
    pub stepper_sig: &'static StepperCmdSignal,
    pub pump_sig: &'static PumpCmdSignal,
    pub stop_sub: StopCmdSub,
}

#[embassy_executor::task]
pub async fn route_cmd(handle_cmd: HandleCmd) {
    let HandleCmd {
        cmd_chan,
        stepper_sig,
        pump_sig,
        mut stop_sub,
    } = handle_cmd;

    let mut stopped = false;

    loop {
        if stopped {
            // Wait for a continue command.
            let cmd = stop_sub.next_message_pure().await;
            match cmd {
                crate::cmd::StopCmd::Continue => {
                    stopped = false;
                }
                _ => {
                    continue;
                }
            }
        }

        let cmd = select_biased! {
            stop_cmd = stop_sub.next_message_pure().fuse() => {
                stopped = stop_cmd == crate::cmd::StopCmd::Immediate;
                continue;
            },
            cmd = cmd_chan.receive().fuse() => cmd,
        };

        match cmd {
            Cmd::Stepper(stepper_cmd) => {
                stepper_sig.send(stepper_cmd).await;
            }
            Cmd::Pump(pump_cmd) => {
                pump_sig.send(pump_cmd).await;
            }
            Cmd::Led() => {
                // TODO
            }
            Cmd::LiftMotor() => {
                // TODO
            }
        }
    }
}
