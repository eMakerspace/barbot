#!/usr/bin/env python3
"""
Barbot – entry point.

Dependency graph:
  main
    ├── BarbotRepository  ← WooClient + StoreConfig + AttributesConfig + BarbotConfig
    ├── InventoryManager  ← BarbotRepository
    ├── DrinkResolver     ← BarbotRepository
    ├── DummyLED          (implements AbstractLED)
    ├── DummyMachine      (implements AbstractMachine; owns scale internally)
    ├── LCDMenu           (pure view – no domain knowledge)
    └── BarbotFSM         ← all of the above
"""

import logging

from config import BarbotConfig, AttributesConfig, HardwareConfig, load_env, init_missing_configs
from store import StoreConfig
from woo_client import WooClient
from repository import BarbotRepository
from inventory import InventoryManager
from mixer import DrinkResolver
from hardware_dummy import DummyLED, DummyMachine
from lcd_menu import LCDMenu
from barbot_fsm import BarbotFSM

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    init_missing_configs()
    env        = load_env()
    config     = BarbotConfig.load()
    hw_config  = HardwareConfig.load()
    store      = StoreConfig.load()
    attributes = AttributesConfig.load()

    woo  = WooClient(env)
    repo = BarbotRepository(woo, store, attributes, config)

    inventory = InventoryManager(repo)
    resolver  = DrinkResolver(repo)

    led     = DummyLED()
    machine = DummyMachine(
        x_max=hw_config.x_max,
        x_idle=hw_config.x_idle,
        pour_duration_ms=hw_config.pour_duration_ms,
        settle_duration_ms=hw_config.settle_duration_ms,
        slot_positions=hw_config.slot_positions,
    )

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
