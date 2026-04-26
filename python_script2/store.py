"""Store configuration: WooCommerce product catalog cache and recipe derivation."""

from datetime import datetime, timezone
from pathlib import Path

from config import STORE_CONFIG_PATH, load_json, save_json
from woo_client import WooClient


class StoreConfig:
    """Cached WooCommerce catalog with recipe/volume derivation."""

    def __init__(self, data: dict, path: Path = STORE_CONFIG_PATH):
        self._path = path
        self._data = data

    @classmethod
    def load(cls, path: Path = STORE_CONFIG_PATH) -> "StoreConfig":
        return cls(load_json(path), path)

    def save(self):
        save_json(self._data, self._path)

    # -- Properties ----------------------------------------------------------

    @property
    def last_fetched(self) -> str | None:
        return self._data.get("last_fetched")


    @property
    def available_spirits(self) -> list[str]:
        return self._data.get("available_spirits", [])

    @property
    def available_mixers(self) -> list[str]:
        return self._data.get("available_mixers", [])

    @property
    def products(self) -> list[dict]:
        return self._data.get("products", [])

    # -- Attribute slug helpers ----------------------------------------------

    @staticmethod
    def attr_slug(display_name: str, attribute_slugs: dict[str, str]) -> str:
        return attribute_slugs.get(display_name, display_name.lower())

    @staticmethod
    def parse_cl_to_ml(val) -> float:
        """Parse a cl value like '4', '4 cl', or ['4 cl'] into ml."""
        if isinstance(val, list):
            val = val[0] if val else "0"
        numeric = "".join(c for c in str(val).split()[0] if c in "0123456789.")
        return float(numeric) * 10 if numeric else 0

    # -- Derived data --------------------------------------------------------

    def product_by_id(self, product_id: int) -> dict | None:
        for p in self.products:
            if p["id"] == product_id:
                return p
        return None

    # -- Fetch from WooCommerce ----------------------------------------------

    def fetch(self, woo: WooClient, term_slugs: dict | None = None):
        """Fetch all products from WooCommerce and rebuild cache.

        term_slugs: {attr_slug: {display_name: slug}} for converting option
        display names to slugs. Fetch attributes first to populate this.
        """
        products_raw = woo.fetch_all("products")

        term_slugs = term_slugs or {}
        spirits: set[str] = set()
        mixers: set[str] = set()
        products = []

        for prod in products_raw:
            attrs_out = []
            for attr in prod.get("attributes", []):
                a_slug = attr.get("slug", attr.get("name", ""))

                if attr.get("options"):
                    raw_values = attr["options"]
                elif attr.get("option"):
                    raw_values = [attr["option"]]
                else:
                    raw_values = []

                # Convert display names to slugs
                slug_map = term_slugs.get(a_slug, {})
                value_slugs = [slug_map.get(v, v) for v in raw_values]

                attrs_out.append({"name": a_slug, "value": value_slugs})

                if a_slug == "pa_spirits":
                    spirits.update(value_slugs)
                elif a_slug == "pa_mixers":
                    mixers.update(value_slugs)

            products.append({
                "id": prod["id"],
                "name": prod["name"],
                "slug": prod.get("slug", ""),
                "sku": prod.get("sku", ""),
                "type": prod["type"],
                "status": prod.get("status", ""),
                "stock_status": prod.get("stock_status", ""),
                "attributes": attrs_out,
            })

        self._data = {
            "last_fetched": datetime.now(timezone.utc).isoformat(),
            "available_spirits": sorted(spirits),
            "available_mixers": sorted(mixers),
            "products": products,
        }
        self.save()
