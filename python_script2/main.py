#!/usr/bin/env python3
"""Barbot – Automated Bartender WooCommerce Integration."""

from config import BarbotConfig, AttributesConfig, load_env, init_missing_configs
from store import StoreConfig
from woo_client import WooClient
from hardware import HardwareInterface
from inventory import InventoryManager
from orders import OrderProcessor
from lcd_menu import LCDMenu


def main():
    try:
        init_missing_configs()
        env = load_env()
        config = BarbotConfig.load()
        store = StoreConfig.load()
        attributes = AttributesConfig.load()
        woo = WooClient(env)
        hardware = HardwareInterface(config)
        inventory = InventoryManager(config, store, attributes, woo)
        orders = OrderProcessor(config, store, attributes, woo, hardware)
        ui = LCDMenu(config, store, attributes, woo, hardware, inventory, orders)
        ui.run()
    except Exception as e:
        # If anything crashes during initialization, the LCD/GPIO cleanup in
        # LCDMenu.run()'s finally block will still execute
        raise


if __name__ == "__main__":
    main()
