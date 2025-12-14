use esp_println::println;

use crate::cmd::{Cmd, CmdChannel, StepperCmd, StepperCmdSignal};

pub struct HandleCmd {
    pub cmd_chan: &'static CmdChannel,
    pub stepper_sig: &'static StepperCmdSignal,
}

#[embassy_executor::task]
pub async fn route_cmd(handle_cmd: HandleCmd) {
    let HandleCmd {
        cmd_chan,
        stepper_sig,
    } = handle_cmd;

    loop {
        let cmd = cmd_chan.receive().await;
        match cmd {
            Cmd::Stepper(stepper_cmd) => {
                stepper_sig.send(stepper_cmd).await;
                println!("stepper cmd finished\r");
            }
            Cmd::Pump() => {
                // TODO
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
