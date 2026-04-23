"""
InventoryManager – synchronises physical slot state with WooCommerce stock.

Depends only on BarbotRepository; never touches WooClient or StoreConfig directly.
Emits no events – the FSM calls push_all() / sync_ingredient() explicitly.
"""

import logging

from repository import BarbotRepository

log = logging.getLogger("INVENTORY")


class InventoryManager:

    def __init__(self, repo: BarbotRepository):
        self._repo = repo

    # ── Public API ────────────────────────────────────────────────────────────

    def push_all(self) -> str:
        """Full sync: set every product/variation in/out of stock based on mounted slots."""
        log.info("[INVENTORY] push_all – syncing all products")
        mounted    = self._repo.config.mounted_ingredients()
        sp_slug    = self._repo.attr_slug("Spirits")
        mx_slug    = self._repo.attr_slug("Mixers")
        products   = self._repo.fetch_all("products")

        product_updates: list[dict]             = []
        variation_updates: dict[int, list[dict]] = {}

        for prod in products:
            if prod["type"] == "simple":
                needed = self._simple_ingredients(prod, sp_slug, mx_slug)
                if not needed:
                    continue
                status = "instock" if needed <= mounted else "outofstock"
                product_updates.append({"id": prod["id"], "stock_status": status})

            elif prod["type"] == "variable":
                updates = self._variable_updates(prod["id"], sp_slug, mx_slug, mounted)
                if updates:
                    variation_updates[prod["id"]] = updates

        self._apply(product_updates, variation_updates)
        log.info("[INVENTORY] push_all complete – %d product updates, %d variable parents",
                 len(product_updates), len(variation_updates))
        return "Inventory synced!"

    def sync_ingredient(self, ingredient: str, in_stock: bool) -> None:
        """Toggle stock for all products/variations that use a specific ingredient."""
        status   = "instock" if in_stock else "outofstock"
        products = self._repo.fetch_all("products")
        recipes  = self._repo.get_preset_recipes()

        product_updates: list[dict]             = []
        variation_updates: dict[int, list[dict]] = {}

        for prod in products:
            if prod["type"] == "simple":
                sku    = (prod.get("sku") or "").upper()
                name   = (prod.get("name") or "").upper()
                recipe = recipes.get(sku) or recipes.get(name)
                if recipe and any(i["name"] == ingredient for i in recipe["ingredients"]):
                    product_updates.append({"id": prod["id"], "stock_status": status})

            elif prod["type"] == "variable":
                variations = self._repo.fetch_all(f"products/{prod['id']}/variations")
                for var in variations:
                    if any(a.get("option") == ingredient for a in var.get("attributes", [])):
                        variation_updates.setdefault(prod["id"], []).append(
                            {"id": var["id"], "stock_status": status}
                        )

        self._apply(product_updates, variation_updates)

    def mark_empty(self, slot: str) -> bool:
        ingredient = self._repo.slot_ingredient(slot)
        if not ingredient:
            return False
        self._repo.config.empty_slots.add(slot)
        self.sync_ingredient(ingredient, in_stock=False)
        return True

    def mark_refill(self, slot: str) -> bool:
        ingredient = self._repo.slot_ingredient(slot)
        if not ingredient:
            return False
        self._repo.config.empty_slots.discard(slot)
        self.sync_ingredient(ingredient, in_stock=True)
        return True

    # ── Private helpers ───────────────────────────────────────────────────────

    def _simple_ingredients(self, prod: dict, sp_slug: str, mx_slug: str) -> set[str]:
        stored = self._repo.product_by_id(prod["id"])
        if stored:
            attrs = {a["name"]: a["value"] for a in stored.get("attributes", [])}
        else:
            attrs = {a["name"]: a.get("options", []) for a in prod.get("attributes", [])}
        return set(attrs.get(sp_slug, [])) | set(attrs.get(mx_slug, []))

    def _variable_updates(
        self, product_id: int, sp_slug: str, mx_slug: str, mounted: set[str]
    ) -> list[dict]:
        updates    = []
        variations = self._repo.fetch_all(f"products/{product_id}/variations")
        for var in variations:
            needed = set()
            for a in var.get("attributes", []):
                attr_slug = a.get("slug", a.get("name", ""))
                option    = a.get("option", "")
                if attr_slug in (sp_slug, mx_slug) and option:
                    term_map = self._repo.term_slugs.get(attr_slug, {})
                    needed.add(term_map.get(option, option))
            if not needed:
                continue
            status = "instock" if needed <= mounted else "outofstock"
            updates.append({"id": var["id"], "stock_status": status})
        return updates

    def _apply(
        self,
        product_updates: list[dict],
        variation_updates: dict[int, list[dict]],
    ) -> None:
        if product_updates:
            self._repo.batch_update_products(product_updates)
        for parent_id, updates in variation_updates.items():
            self._repo.batch_update_variations(parent_id, updates)
