#!/usr/bin/env python3
"""Barbot – Automated Bartender WooCommerce Integration."""

import sys
import signal
from config import BarbotConfig, AttributesConfig, HardwareConfig, load_env, init_missing_configs
from store import StoreConfig
from woo_client import WooClient
from hardware import HardwareInterface
from inventory import InventoryManager
from orders import OrderProcessor
from lcd_menu import LCDMenu
from logger import log_info, log_error, log_critical


_hardware_instance = None  # Global reference for signal handler


def cleanup_on_signal(signum, frame):
    """Handle Ctrl+C gracefully by cleaning up hardware."""
    global _hardware_instance
    log_critical("MAIN", "=== KEYBOARD INTERRUPT RECEIVED ===")
    if _hardware_instance:
        log_info("MAIN", "Cleaning up hardware...")
        try:
            _hardware_instance.cleanup()
            log_info("MAIN", "Hardware cleanup complete")
        except Exception as e:
            log_error("MAIN", f"Error during cleanup: {e}")
    sys.exit(0)


def main():
    global _hardware_instance

    try:
        log_info("MAIN", "=== BARBOT STARTUP ===")

        log_info("MAIN", "Initializing configuration...")
        init_missing_configs()
        env = load_env()
        log_info("MAIN", f"Environment loaded: {len(env)} variables")

        config = BarbotConfig.load()
        log_info("MAIN", f"Barbot config loaded: poll_interval={config.poll_interval}s")

        hw_config = HardwareConfig.load()
        log_info("MAIN", f"Hardware config loaded: serial_port={hw_config.serial_port}")

        store = StoreConfig.load()
        log_info("MAIN", f"Store config loaded: {len(store.products)} products cached")

        attributes = AttributesConfig.load()
        log_info("MAIN", f"Attributes config loaded: {len(attributes.attribute_ids)} attributes")

        log_info("MAIN", "Initializing WooCommerce client...")
        woo = WooClient(env)
        log_info("MAIN", "WooCommerce client created")

        log_info("MAIN", "Initializing hardware interface...")
        hardware = HardwareInterface(config, hw_config)
        _hardware_instance = hardware
        log_info("MAIN", f"Hardware interface ready: GPIO={'ready' if hardware._gpio_ready else 'unavailable'}, Serial={'connected' if hardware._esp else 'unavailable'}")

        log_info("MAIN", "Initializing inventory manager...")
        inventory = InventoryManager(config, store, attributes, woo)
        log_info("MAIN", "Inventory manager ready")

        log_info("MAIN", "Initializing order processor...")
        orders = OrderProcessor(config, store, attributes, woo, hardware)
        log_info("MAIN", "Order processor ready")

        log_info("MAIN", "Initializing LCD menu...")
        ui = LCDMenu(config, store, attributes, woo, hardware, inventory, orders)
        orders.ui = ui
        hardware.ui = ui  # inject UI reference into hardware for LCD messages
        log_info("MAIN", "LCD menu ready")

        # Register signal handler for graceful Ctrl+C shutdown
        signal.signal(signal.SIGINT, cleanup_on_signal)

        log_info("MAIN", "Starting UI run loop...")
        ui.run()

    except KeyboardInterrupt:
        log_critical("MAIN", "KeyboardInterrupt caught in main, cleaning up...")
        if _hardware_instance:
            try:
                _hardware_instance.cleanup()
                log_info("MAIN", "Hardware cleanup complete after interrupt")
            except Exception as e:
                log_error("MAIN", f"Error during cleanup: {e}")
        sys.exit(0)
    except Exception as e:
        log_error("MAIN", f"Exception during startup: {type(e).__name__}: {e}")
        if _hardware_instance:
            try:
                _hardware_instance.cleanup()
            except Exception as cleanup_err:
                log_error("MAIN", f"Error during cleanup: {cleanup_err}")
        raise


if __name__ == "__main__":
    main()
