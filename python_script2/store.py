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

    def all_ingredients(self) -> set[str]:
        return set(self.available_spirits) | set(self.available_mixers)

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

    def get_preset_recipes(self, attribute_slugs: dict[str, str]) -> dict:
        """Build {KEY: {ingredients, product_id}} from simple products."""
        spirits_slug = self.attr_slug("Spirits", attribute_slugs)
        mixers_slug = self.attr_slug("Mixers", attribute_slugs)
        spirits_amt_slug = self.attr_slug("Spirits Amount", attribute_slugs)
        mixers_amt_slug = self.attr_slug("Mixers Amount", attribute_slugs)

        recipes = {}
        for prod in self.products:
            if prod.get("type") != "simple":
                continue
            attrs = {a["name"]: a["value"] for a in prod.get("attributes", [])}
            ingredients = []

            spirit_vals = attrs.get(spirits_slug, [])
            spirit_amt = attrs.get(spirits_amt_slug, [])
            mixer_vals = attrs.get(mixers_slug, [])
            mixer_amt = attrs.get(mixers_amt_slug, [])

            if spirit_vals and spirit_amt:
                ml = self.parse_cl_to_ml(spirit_amt)
                for s in (spirit_vals if isinstance(spirit_vals, list) else [spirit_vals]):
                    ingredients.append({"name": s, "ml": ml})
            if mixer_vals and mixer_amt:
                ml = self.parse_cl_to_ml(mixer_amt)
                for m in (mixer_vals if isinstance(mixer_vals, list) else [mixer_vals]):
                    ingredients.append({"name": m, "ml": ml})

            if ingredients:
                key = (prod.get("sku") or prod.get("slug") or prod["name"]).upper()
                recipes[key] = {"ingredients": ingredients, "product_id": prod["id"]}
        return recipes

    def get_diy_volumes(self, attribute_slugs: dict[str, str]) -> dict:
        """Extract default Spirit/Mixer volumes (ml) from the first variable product."""
        spirits_amt_slug = self.attr_slug("Spirits Amount", attribute_slugs)
        mixers_amt_slug = self.attr_slug("Mixers Amount", attribute_slugs)

        for prod in self.products:
            if prod.get("type") != "variable":
                continue
            attrs = {a["name"]: a["value"] for a in prod.get("attributes", [])}
            spirit_ml = self.parse_cl_to_ml(attrs.get(spirits_amt_slug, ["4"]))
            mixer_ml = self.parse_cl_to_ml(attrs.get(mixers_amt_slug, ["16"]))
            if spirit_ml or mixer_ml:
                return {"Spirit": spirit_ml, "Mixer": mixer_ml}
        return {"Spirit": 40, "Mixer": 160}

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
        print("\n[FETCH] Fetching products...")
        products_raw = woo.fetch_all("products")
        print(f"  [FETCH] Got {len(products_raw)} product(s).")

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

        print(f"  [FETCH] Spirits:  {self.available_spirits}")
        print(f"  [FETCH] Mixers:   {self.available_mixers}")
        print(f"  [FETCH] Saved to store_config.json ({len(products)} products).")
