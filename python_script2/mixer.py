"""Drink recipe resolution: translate a WooCommerce line item into dispense instructions."""

from dataclasses import dataclass, field

from config import BarbotConfig, AttributesConfig
from store import StoreConfig

DIY_SKU_PREFIX = "DRINK-410"


@dataclass
class DrinkSpec:
    """Resolved dispense plan for one drink."""
    name: str
    spirits: list[dict] = field(default_factory=list)   # [{ingredient, slot, pours}]
    mixers: list[dict] = field(default_factory=list)    # [{ingredient, slot, ml}]

    def log(self):
        print(f"[DRINK] {self.name}")
        for s in self.spirits:
            print(f"  Spirit : {s['ingredient']} → slot {s['slot']} × {s['pours']} pour(s)")
        for m in self.mixers:
            print(f"  Mixer  : {m['ingredient']} → slot {m['slot']} | {m['ml']:.1f} ml (viscosity-adjusted)")


class DrinkResolver:
    """Resolves line items into DrinkSpec objects."""

    def __init__(self, config: BarbotConfig, store: StoreConfig, attributes: AttributesConfig):
        self.config = config
        self.store = store
        self.attributes = attributes
        self._spirit_measure_cl = float(
            self._get_spirit_measure_cl()
        )

    def _get_spirit_measure_cl(self) -> float:
        from config import load_json, SLOTS_CONFIG_PATH
        data = load_json(SLOTS_CONFIG_PATH)
        return float(data.get("spirit_measures_cl", 2))

    def _spirit_viscosity(self, slug: str) -> float:
        props = self.attributes.bottle_properties.get("pa_spirits", {})
        return props.get(slug, {}).get("viscosity", 1.0)

    def _mixer_viscosity(self, slug: str) -> float:
        props = self.attributes.bottle_properties.get("pa_mixers", {})
        return props.get(slug, {}).get("viscosity", 1.0)

    def _product_attrs(self, product_id: int) -> dict:
        """Return {attr_name: value_list} for a product from the store cache."""
        prod = self.store.product_by_id(product_id)
        if not prod:
            return {}
        return {a["name"]: a["value"] for a in prod.get("attributes", [])}

    def resolve(self, item: dict) -> list["DrinkSpec"]:
        """Return one DrinkSpec per unit (quantity) of the line item."""
        quantity = item.get("quantity", 1)
        spec = self._resolve_one(item)
        return [spec] * quantity

    def _resolve_one(self, item: dict) -> "DrinkSpec":
        sku = (item.get("sku") or "").upper()
        name = item.get("name", sku)
        product_id = item.get("product_id")

        # DIY variable product: spirits/mixers come from line item meta
        if sku.startswith(DIY_SKU_PREFIX) or item.get("variation_id", 0):
            meta = {m["key"]: m["value"] for m in item.get("meta_data", [])}
            spirit_slug = meta.get("pa_spirits")
            mixer_slug = meta.get("pa_mixers")

            # Volumes from the variable product attributes in the store cache
            attrs = self._product_attrs(item.get("product_id", 0))
            spirit_cl = StoreConfig.parse_cl_to_ml(attrs.get("pa_spirits-amount", ["4"])) / 10
            mixer_cl = StoreConfig.parse_cl_to_ml(attrs.get("pa_mixers-amount", ["21"])) / 10

            spirits = self._build_spirits([(spirit_slug, spirit_cl)] if spirit_slug else [])
            mixers = self._build_mixers([(mixer_slug, mixer_cl)] if mixer_slug else [])
            return DrinkSpec(name=name, spirits=spirits, mixers=mixers)

        # Preset simple product: look up by SKU in store cache
        attrs = self._product_attrs(product_id) if product_id else {}
        if not attrs:
            # Fallback: find by SKU
            for prod in self.store.products:
                if (prod.get("sku") or "").upper() == sku:
                    attrs = {a["name"]: a["value"] for a in prod.get("attributes", [])}
                    break

        spirit_slugs = attrs.get("pa_spirits", [])
        mixer_slugs = attrs.get("pa_mixers", [])
        spirit_cl = StoreConfig.parse_cl_to_ml(attrs.get("pa_spirits-amount", ["4"])) / 10
        mixer_cl = StoreConfig.parse_cl_to_ml(attrs.get("pa_mixers-amount", ["21"])) / 10

        spirits = self._build_spirits([(s, spirit_cl) for s in spirit_slugs])
        mixers = self._build_mixers([(m, mixer_cl) for m in mixer_slugs])
        return DrinkSpec(name=name, spirits=spirits, mixers=mixers)

    def _build_spirits(self, entries: list[tuple[str, float]]) -> list[dict]:
        result = []
        for slug, cl in entries:
            slot = self.config.ingredient_to_slot.get(slug)
            pours = round(cl / self._spirit_measure_cl)
            viscosity = self._spirit_viscosity(slug)
            result.append({"ingredient": slug, "slot": slot, "pours": pours, "viscosity": viscosity})
        return result

    def _build_mixers(self, entries: list[tuple[str, float]]) -> list[dict]:
        result = []
        for slug, cl in entries:
            slot = self.config.ingredient_to_slot.get(slug)
            viscosity = self._mixer_viscosity(slug)
            ml = cl * 10 * viscosity  # cl → ml, then × viscosity
            result.append({"ingredient": slug, "slot": slot, "ml": ml})
        return result
