"""Inventory management: push full stock state and per-ingredient sync."""

from config import BarbotConfig, AttributesConfig
from store import StoreConfig
from woo_client import WooClient


class InventoryManager:
    """Manages WooCommerce stock status based on physical slot state."""

    def __init__(self, config: BarbotConfig, store: StoreConfig, attributes: AttributesConfig, woo: WooClient):
        self.config = config
        self.store = store
        self.attributes = attributes
        self.woo = woo

    # -- Public API ----------------------------------------------------------

    def push_all(self):
        """Full sync: set every product/variation in/out of stock based on mounted slots."""
        mounted = self.config.mounted_ingredients()
        attr_slugs = self.attributes.attribute_slugs
        spirits_slug = self.store.attr_slug("Spirits", attr_slugs)
        mixers_slug = self.store.attr_slug("Mixers", attr_slugs)


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
        """Build stock updates for all variations of a variable product.

        WooCommerce returns variation attribute options as display names,
        so we convert them to slugs before comparing against mounted (slugs).
        """
        updates = []
        variations = self.woo.fetch_all(f"products/{product_id}/variations")
        for var in variations:
            needed = set()
            for a in var.get("attributes", []):
                attr_slug = a.get("slug", a.get("name", ""))
                option = a.get("option", "")
                if attr_slug in (spirits_slug, mixers_slug) and option:
                    # Convert display name -> slug for comparison
                    term_map = self.attributes.term_slugs.get(attr_slug, {})
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
            self.woo.batch_update_products(product_updates)

        for parent_id, updates in variation_updates.items():
            self.woo.batch_update_variations(parent_id, updates)
