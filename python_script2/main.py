#!/usr/bin/env python3
"""
Barbot – entry point.

Dependency graph:
  main
    ├── BarbotRepository  ← WooClient + StoreConfig + AttributesConfig + BarbotConfig
    ├── InventoryManager  ← BarbotRepository
    ├── DrinkResolver     ← BarbotRepository
    ├── RealLED           (implements AbstractLED)
    ├── RealMachine       (implements AbstractMachine; owns scale internally)
    ├── LCDMenu           (pure view – no domain knowledge)
    └── BarbotFSM         ← all of the above
"""

import logging

from config import BarbotConfig, AttributesConfig, HardwareConfig, load_env, init_missing_configs
from serial_probe import probe_and_update
from store import StoreConfig
from woo_client import WooClient
from repository import BarbotRepository
from inventory import InventoryManager
from mixer import DrinkResolver
from real_hardware import RealLED, RealMachine
from lcd_menu import LCDMenu
from barbot_fsm import BarbotFSM

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    init_missing_configs()

    # Probe serial ports – detected ports are written back into hardware_config.json.
    found = probe_and_update()

    env        = load_env()
    config     = BarbotConfig.load()
    hw_config  = HardwareConfig.load()   # load after probe so ports are up-to-date
    store      = StoreConfig.load()
    attributes = AttributesConfig.load()

    woo  = WooClient(env)
    repo = BarbotRepository(woo, store, attributes, config)

    inventory = InventoryManager(repo)
    resolver  = DrinkResolver(repo)

    led     = RealLED(hw_config.display_port, hw_config.display_baud)
    machine = RealMachine(hw_config)

    ui = LCDMenu()

    fsm = BarbotFSM(
        led=led,
        machine=machine,
        repo=repo,
        inventory=inventory,
        resolver=resolver,
        ui=ui,
    )

    fsm.run()


if __name__ == "__main__":
    main()
