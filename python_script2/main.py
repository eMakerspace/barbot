#!/usr/bin/env python3
"""Barbot – Automated Bartender WooCommerce Integration."""

from config import BarbotConfig, load_env
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
    woo = WooClient(env)
    hardware = HardwareInterface(config)
    inventory = InventoryManager(config, store, woo)
    orders = OrderProcessor(config, store, woo, hardware)
    console = Console(config, store, woo, hardware, inventory, orders)
    console.run()


if __name__ == "__main__":
    main()
