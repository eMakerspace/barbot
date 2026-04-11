"""Order polling, parsing, and processing."""

import threading
import time

from config import BarbotConfig, AttributesConfig
from store import StoreConfig
from woo_client import WooClient
from hardware import HardwareInterface
from mixer import DrinkResolver
from progress import OrderProgress

HEARTBEAT_INTERVAL = 15  # seconds between heartbeats during mixing


class OrderProcessor:
    """Fetches, parses, and executes WooCommerce orders."""

    def __init__(
        self,
        config: BarbotConfig,
        store: StoreConfig,
        attributes: AttributesConfig,
        woo: WooClient,
        hardware: HardwareInterface,
        ui=None,  # LCDMenu reference, injected after construction to avoid circular import
    ):
        self.config = config
        self.store = store
        self.attributes = attributes
        self.woo = woo
        self.hw = hardware
        self.ui = ui
        self.resolver = DrinkResolver(config, store, attributes)
        self.progress = OrderProgress()
        self._heartbeat_stop = threading.Event()

    def _heartbeat_thread(self):
        """Send heartbeats at a fixed interval until _heartbeat_stop is set."""
        while not self._heartbeat_stop.wait(HEARTBEAT_INTERVAL):
            self.woo.send_heartbeat()

    def _start_heartbeat(self):
        self._heartbeat_stop.clear()
        t = threading.Thread(target=self._heartbeat_thread, daemon=True)
        t.start()
        return t

    def _stop_heartbeat(self, thread: threading.Thread):
        self._heartbeat_stop.set()
        thread.join(timeout=HEARTBEAT_INTERVAL + 5)

    def process_order(self, order: dict):
        """Parse all line items, make each drink, then mark order completed."""
        order_id = order["id"]
        short_id = order_id % 100
        line_items = order.get("line_items", [])

        # Expand all line items into an ordered flat list of DrinkSpecs
        all_specs = []
        for item in line_items:
            all_specs.extend(self.resolver.resolve(item))

        total = len(all_specs)
        print(f"[ORDER #{order_id}] {total} drink(s) in {len(line_items)} line item(s)")

        # Check for saved progress from a previous interrupted run
        skip = self.progress.resume_from_disk(order_id, total)
        if skip == 0:
            self.progress.start(order_id, total)

        self.hw.display_order_id(short_id)

        # Keep sending heartbeats while mixing so the website doesn't lock up
        hb_thread = self._start_heartbeat()
        try:
            for drink_num, spec in enumerate(all_specs, start=1):
                if drink_num <= skip:
                    print(f"[ORDER #{order_id}] Skipping drink {drink_num}/{total} (already made)")
                    continue

                print(f"[ORDER #{order_id}] Making drink {drink_num}/{total}:")
                spec.log()
                if self.ui:
                    self.ui.show_mixing(drink_num, total, spec.name)
                self.hw.make_drink(spec)
                self.progress.drink_done()
        finally:
            self._stop_heartbeat(hb_thread)

        if self.ui:
            self.ui.clear_mixing()
        self.progress.clear()
        self.woo.update_order_status(order_id, "completed")
        print(f"[ORDER #{order_id}] Done – status set to completed")

    def poll_loop(self, interval: int):
        """Blocking loop: heartbeat + fetch processing orders."""
        while True:
            self.woo.send_heartbeat()
            try:
                orders = self.woo.fetch_all("orders", {"status": "processing"})
                for order in orders:
                    self.process_order(order)
            except Exception:
                pass
            time.sleep(interval)
