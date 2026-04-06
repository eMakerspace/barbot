#!/usr/bin/env python3
"""Barbot – Automated Bartender WooCommerce Integration."""

from config import BarbotConfig, AttributesConfig, load_env
from store import StoreConfig
from woo_client import WooClient
from hardware import HardwareInterface
from inventory import InventoryManager
from orders import OrderProcessor
from console import Console


def main():
    env = load_env()
    config = BarbotConfig.load()
    store = StoreConfig.load()
    attributes = AttributesConfig.load()
    woo = WooClient(env)
    hardware = HardwareInterface(config)
    inventory = InventoryManager(config, store, attributes, woo)
    orders = OrderProcessor(config, store, attributes, woo, hardware)
    console = Console(config, store, attributes, woo, hardware, inventory, orders)
    console.run()


if __name__ == "__main__":
    main()
