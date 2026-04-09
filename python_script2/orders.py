"""Order polling, parsing, and processing."""

import time

from config import BarbotConfig, AttributesConfig
from store import StoreConfig
from woo_client import WooClient
from hardware import HardwareInterface
from mixer import DrinkResolver


class OrderProcessor:
    """Fetches, parses, and executes WooCommerce orders."""

    def __init__(
        self,
        config: BarbotConfig,
        store: StoreConfig,
        attributes: AttributesConfig,
        woo: WooClient,
        hardware: HardwareInterface,
    ):
        self.config = config
        self.store = store
        self.attributes = attributes
        self.woo = woo
        self.hw = hardware
        self.resolver = DrinkResolver(config, store, attributes)

    def process_order(self, order: dict):
        """Parse all line items, make each drink, then mark order completed."""
        order_id = order["id"]
        short_id = order_id % 100
        line_items = order.get("line_items", [])

        # Count total drinks
        total = sum(item.get("quantity", 1) for item in line_items)
        print(f"[ORDER #{order_id}] {total} drink(s) in {len(line_items)} line item(s)")

        self.hw.display_order_id(short_id)

        drink_num = 0
        for item in line_items:
            specs = self.resolver.resolve(item)
            for spec in specs:
                drink_num += 1
                print(f"[ORDER #{order_id}] Making drink {drink_num}/{total}:")
                spec.log()
                self.hw.make_drink(spec)

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
