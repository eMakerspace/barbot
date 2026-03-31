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
    def attribute_slugs(self) -> dict[str, str]:
        return self._data.get("attribute_slugs", {})

    @property
    def term_slugs(self) -> dict[str, dict[str, str]]:
        return self._data.get("term_slugs", {})

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

    def attr_slug(self, display_name: str) -> str:
        return self.attribute_slugs.get(display_name, display_name.lower())

    @staticmethod
    def parse_cl_to_ml(val) -> float:
        """Parse a cl value like '4', '4 cl', or ['4 cl'] into ml."""
        if isinstance(val, list):
            val = val[0] if val else "0"
        numeric = "".join(c for c in str(val).split()[0] if c in "0123456789.")
        return float(numeric) * 10 if numeric else 0

    # -- Derived data --------------------------------------------------------

    def get_preset_recipes(self) -> dict:
        """Build {KEY: {ingredients, product_id}} from simple products."""
        spirits_slug = self.attr_slug("Spirits")
        mixers_slug = self.attr_slug("Mixers")
        spirits_amt_slug = self.attr_slug("Spirits Amount")
        mixers_amt_slug = self.attr_slug("Mixers Amount")

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

    def get_diy_volumes(self) -> dict:
        """Extract default Spirit/Mixer volumes (ml) from the first variable product."""
        spirits_amt_slug = self.attr_slug("Spirits Amount")
        mixers_amt_slug = self.attr_slug("Mixers Amount")

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

    def fetch(self, woo: WooClient):
        """Pull all products and attribute terms from WooCommerce, rebuild cache."""
        print("\n[FETCH] Fetching product attributes and terms ...")
        attr_slug_by_name, term_slugs = self._fetch_attribute_terms(woo)
        print(f"  [FETCH] Got {len(attr_slug_by_name)} attribute(s): {list(attr_slug_by_name.keys())}")

        print("[FETCH] Fetching products ...")
        products_raw = woo.fetch_all("products")
        print(f"  [FETCH] Got {len(products_raw)} product(s).")

        spirits: set[str] = set()
        mixers: set[str] = set()
        products = []

        spirits_key = attr_slug_by_name.get("Spirits", "spirits")
        mixers_key = attr_slug_by_name.get("Mixers", "mixers")

        for prod in products_raw:
            attrs_out = []
            for attr in prod.get("attributes", []):
                a_name = attr.get("name", "")
                a_slug = attr_slug_by_name.get(a_name, a_name)
                terms_map = term_slugs.get(a_slug, {})

                if attr.get("options"):
                    raw_values = attr["options"]
                elif attr.get("option"):
                    raw_values = [attr["option"]]
                else:
                    raw_values = []

                value_slugs = [terms_map.get(v, v) for v in raw_values]
                attrs_out.append({"name": a_slug, "value": value_slugs})

                if a_slug == spirits_key:
                    spirits.update(value_slugs)
                elif a_slug == mixers_key:
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
            "attribute_slugs": attr_slug_by_name,
            "term_slugs": term_slugs,
            "available_spirits": sorted(spirits),
            "available_mixers": sorted(mixers),
            "products": products,
        }
        self.save()

        print(f"  [FETCH] Spirits:  {self.available_spirits}")
        print(f"  [FETCH] Mixers:   {self.available_mixers}")
        print(f"  [FETCH] Saved to store_config.json ({len(products)} products).")

    @staticmethod
    def _fetch_attribute_terms(woo: WooClient) -> tuple[dict, dict]:
        attr_slug_by_name: dict[str, str] = {}
        term_slugs: dict[str, dict[str, str]] = {}

        attributes = woo.fetch_all("products/attributes")
        for attr in attributes:
            a_name = attr["name"]
            a_slug = attr["slug"]
            attr_slug_by_name[a_name] = a_slug
            terms = woo.fetch_all(f"products/attributes/{attr['id']}/terms")
            term_slugs[a_slug] = {t["name"]: t["slug"] for t in terms}

        return attr_slug_by_name, term_slugs
