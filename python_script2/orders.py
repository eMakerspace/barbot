"""Order polling, parsing, and processing."""

import time

from config import BarbotConfig, AttributesConfig
from store import StoreConfig
from woo_client import WooClient
from hardware import HardwareInterface


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

    def parse_line_item(self, item: dict) -> list[dict]:
        """Return [{slot, ingredient, ml}, ...] for one order line item."""
        sku = (item.get("sku") or "").upper()
        name = (item.get("name") or "").upper()
        quantity = item.get("quantity", 1)

        recipes = self.store.get_preset_recipes(self.attributes.attribute_slugs)
        recipe = recipes.get(sku) or recipes.get(name)

        if recipe:
            pours = []
            for ing in recipe["ingredients"]:
                slot = self.config.ingredient_to_slot.get(ing["name"])
                if slot is None:
                    print(f"  [WARN] No slot for ingredient '{ing['name']}'")
                    continue
                pours.append({"slot": slot, "ingredient": ing["name"], "ml": ing["ml"] * quantity})
            return pours

        # DIY variable product
        meta = {m["key"]: m["value"] for m in item.get("meta_data", [])}
        diy_vols = self.store.get_diy_volumes(self.attributes.attribute_slugs)
        pours = []

        for key, ingredient_name in meta.items():
            key_lower = key.lower()
            if "spirit" in key_lower:
                vol = diy_vols.get("Spirit", 40)
            elif "mixer" in key_lower:
                vol = diy_vols.get("Mixer", 160)
            else:
                continue
            slot = self.config.ingredient_to_slot.get(ingredient_name)
            if slot is None:
                print(f"  [WARN] No slot for DIY ingredient '{ingredient_name}'")
                continue
            pours.append({"slot": slot, "ingredient": ingredient_name, "ml": vol * quantity})

        return pours

    def process_order(self, order: dict):
        """Parse, dispense, and mark a single order completed."""
        order_id = order["id"]
        short_id = order_id % 100

        print(f"\n{'='*50}")
        print(f"  Processing Order #{order_id}  (Display: {short_id:02d})")
        print(f"{'='*50}")
        self.hw.display_order_id(short_id)

        for item in order.get("line_items", []):
            print(f"\n  Item: {item['name']} (SKU: {item.get('sku', 'N/A')}) x{item['quantity']}")
            pours = self.parse_line_item(item)
            if not pours:
                print("  [WARN] No pours resolved for this item.")
                continue
            for p in pours:
                self.hw.dispense(p["slot"], p["ml"])

        self.hw.signal_done()

        if self.woo.update_order_status(order_id, "completed"):
            print(f"  [API] Order #{order_id} marked completed.")
        else:
            print(f"  [ERROR] Failed to mark order #{order_id} completed!")

    def poll_loop(self, interval: int):
        """Blocking loop: heartbeat + fetch processing orders."""
        print(f"\n[POLL] Starting order poll (every {interval}s). Press Ctrl-C to stop.\n")
        while True:
            self.woo.send_heartbeat()
            try:
                orders = self.woo.fetch_all("orders", {"status": "processing"})
                if orders:
                    print(f"[POLL] Found {len(orders)} order(s) to process.")
                    for order in orders:
                        self.process_order(order)
                else:
                    print("[POLL] No pending orders.")
            except Exception as e:
                print(f"[POLL] Error during poll: {e}")
            time.sleep(interval)
