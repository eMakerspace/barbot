"""Inventory management: push full stock state and per-ingredient sync."""

from config import BarbotConfig
from store import StoreConfig
from woo_client import WooClient


class InventoryManager:
    """Manages WooCommerce stock status based on physical slot state."""

    def __init__(self, config: BarbotConfig, store: StoreConfig, woo: WooClient):
        self.config = config
        self.store = store
        self.woo = woo

    # -- Public API ----------------------------------------------------------

    def push_all(self):
        """Full sync: set every product/variation in/out of stock based on mounted slots."""
        mounted = self.config.mounted_ingredients()
        spirits_slug = self.store.attr_slug("Spirits")
        mixers_slug = self.store.attr_slug("Mixers")

        print(f"\n[PUSH] Mounted ingredients: {sorted(mounted)}")
        print("[PUSH] Fetching products from WooCommerce ...")

        products = self.woo.fetch_all("products")
        product_updates: list[dict] = []
        variation_updates: dict[int, list[dict]] = {}

        for prod in products:
            if prod["type"] == "simple":
                needed = self._simple_product_ingredients(prod, spirits_slug, mixers_slug)
                if not needed:
                    continue
                status = "instock" if needed <= mounted else "outofstock"
                product_updates.append({"id": prod["id"], "stock_status": status})

            elif prod["type"] == "variable":
                var_updates = self._variable_product_updates(
                    prod["id"], spirits_slug, mixers_slug, mounted
                )
                if var_updates:
                    variation_updates[prod["id"]] = var_updates

        self._apply_updates(product_updates, variation_updates, verbose=True)
        print("[PUSH] Done.")

    def sync_ingredient(self, ingredient: str, in_stock: bool):
        """Toggle stock for all products/variations using a specific ingredient."""
        status = "instock" if in_stock else "outofstock"
        action = "Restocking" if in_stock else "Disabling"
        print(f"\n[INVENTORY] {action} products containing '{ingredient}' ...")

        products = self.woo.fetch_all("products")
        recipes = self.store.get_preset_recipes()
        product_updates: list[dict] = []
        variation_updates: dict[int, list[dict]] = {}

        for prod in products:
            if prod["type"] == "simple":
                sku = (prod.get("sku") or "").upper()
                name = (prod.get("name") or "").upper()
                recipe = recipes.get(sku) or recipes.get(name)
                if recipe and any(i["name"] == ingredient for i in recipe["ingredients"]):
                    product_updates.append({"id": prod["id"], "stock_status": status})

            elif prod["type"] == "variable":
                variations = self.woo.fetch_all(f"products/{prod['id']}/variations")
                for var in variations:
                    if any(a.get("option") == ingredient for a in var.get("attributes", [])):
                        variation_updates.setdefault(prod["id"], []).append(
                            {"id": var["id"], "stock_status": status}
                        )

        self._apply_updates(product_updates, variation_updates)

    def mark_empty(self, slot: str) -> bool:
        """Mark slot empty and sync WooCommerce. Returns False if slot invalid."""
        if not self.config.is_valid_slot(slot):
            print(f"  Invalid slot '{slot}'.")
            return False
        ingredient = self.config.slot_ingredient(slot)
        if not ingredient:
            print(f"  Slot '{slot}' has no ingredient assigned.")
            return False
        self.config.empty_slots.add(slot)
        print(f"  Marked {slot} ({ingredient}) as EMPTY.")
        self.sync_ingredient(ingredient, in_stock=False)
        return True

    def mark_refill(self, slot: str) -> bool:
        """Mark slot refilled and sync WooCommerce. Returns False if slot invalid."""
        if not self.config.is_valid_slot(slot):
            print(f"  Invalid slot '{slot}'.")
            return False
        ingredient = self.config.slot_ingredient(slot)
        if not ingredient:
            print(f"  Slot '{slot}' has no ingredient assigned.")
            return False
        self.config.empty_slots.discard(slot)
        print(f"  Marked {slot} ({ingredient}) as REFILLED.")
        self.sync_ingredient(ingredient, in_stock=True)
        return True

    # -- Private helpers -----------------------------------------------------

    def _simple_product_ingredients(
        self, prod: dict, spirits_slug: str, mixers_slug: str
    ) -> set[str]:
        """Get required ingredient slugs for a simple product."""
        stored = self.store.product_by_id(prod["id"])
        if stored:
            attrs = {a["name"]: a["value"] for a in stored.get("attributes", [])}
        else:
            attrs = {a["name"]: a.get("options", []) for a in prod.get("attributes", [])}
        spirits = attrs.get(spirits_slug, [])
        mixers = attrs.get(mixers_slug, [])
        return set(spirits) | set(mixers)

    def _variable_product_updates(
        self, product_id: int, spirits_slug: str, mixers_slug: str,
        mounted: set[str]
    ) -> list[dict]:
        """Build stock updates for all variations of a variable product."""
        updates = []
        variations = self.woo.fetch_all(f"products/{product_id}/variations")
        for var in variations:
            needed = set()
            for a in var.get("attributes", []):
                slug = a.get("slug", a.get("name", ""))
                option = a.get("option", "")
                if slug in (spirits_slug, mixers_slug) and option:
                    term_map = self.store.term_slugs.get(slug, {})
                    needed.add(term_map.get(option, option))
            if not needed:
                continue
            status = "instock" if needed <= mounted else "outofstock"
            updates.append({"id": var["id"], "stock_status": status})
        return updates

    def _apply_updates(
        self,
        product_updates: list[dict],
        variation_updates: dict[int, list[dict]],
        verbose: bool = False,
    ):
        """Send batch updates to WooCommerce."""
        if product_updates:
            in_c = sum(1 for u in product_updates if u["stock_status"] == "instock")
            out_c = len(product_updates) - in_c
            if self.woo.batch_update_products(product_updates):
                if verbose:
                    print(f"  [API] Updated {len(product_updates)} simple product(s): {in_c} instock, {out_c} outofstock.")
                else:
                    print(f"  [API] Updated {len(product_updates)} simple product(s).")

        total_in = total_out = 0
        for parent_id, updates in variation_updates.items():
            v_in = sum(1 for u in updates if u["stock_status"] == "instock")
            v_out = len(updates) - v_in
            total_in += v_in
            total_out += v_out
            if self.woo.batch_update_variations(parent_id, updates) and verbose:
                print(f"  [API] Product #{parent_id}: {v_in} variation(s) instock, {v_out} outofstock.")

        if verbose and variation_updates:
            print(f"  [API] Total variations: {total_in} instock, {total_out} outofstock.")

        if not product_updates and not variation_updates:
            print("  [INVENTORY] No affected products found.")
