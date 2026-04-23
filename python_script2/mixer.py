"""
DrinkResolver – translates a WooCommerce line item into dispense instructions.

Depends only on BarbotRepository; never touches WooClient or StoreConfig directly.
"""

import logging
from dataclasses import dataclass, field

from repository import BarbotRepository

log = logging.getLogger("MIXER")

DIY_SKU_PREFIX = "DRINK-410"


@dataclass
class DrinkSpec:
    """Resolved dispense plan for one drink."""
    name:    str
    spirits: list[dict] = field(default_factory=list)  # {ingredient, slot, pours, viscosity}
    mixers:  list[dict] = field(default_factory=list)   # {ingredient, slot, ml}

    def log(self) -> None:
        log.info("[DRINK] '%s'", self.name)
        for s in self.spirits:
            log.info("  Spirit  slot=%-8s  pours=%d  visc=%.2f  ingredient=%s",
                     s['slot'], s['pours'], s.get('viscosity', 1.0), s['ingredient'])
        for m in self.mixers:
            log.info("  Mixer   slot=%-8s  ml=%.1f  ingredient=%s",
                     m['slot'], m['ml'], m['ingredient'])


class DrinkResolver:
    """Resolves WooCommerce line items into DrinkSpec objects."""

    def __init__(self, repo: BarbotRepository):
        self._repo = repo

    def resolve(self, item: dict) -> list[DrinkSpec]:
        """Return one DrinkSpec per unit (quantity) of the line item."""
        quantity = item.get("quantity", 1)
        spec     = self._resolve_one(item)
        return [spec] * quantity

    def _resolve_one(self, item: dict) -> DrinkSpec:
        sku        = (item.get("sku") or "").upper()
        name       = item.get("name", sku)
        product_id = item.get("product_id")

        if sku.startswith(DIY_SKU_PREFIX) or item.get("variation_id", 0):
            meta         = {m["key"]: m["value"] for m in item.get("meta_data", [])}
            spirit_slug  = meta.get("pa_spirits")
            mixer_slug   = meta.get("pa_mixers")
            attrs        = self._product_attrs(product_id or 0)
            spirit_cl    = self._repo.parse_cl_to_ml(attrs.get("pa_spirits-amount", ["4"])) / 10
            mixer_cl     = self._repo.parse_cl_to_ml(attrs.get("pa_mixers-amount", ["21"])) / 10
            spirits      = self._build_spirits([(spirit_slug, spirit_cl)] if spirit_slug else [])
            mixers       = self._build_mixers([(mixer_slug, mixer_cl)]   if mixer_slug  else [])
            return DrinkSpec(name=name, spirits=spirits, mixers=mixers)

        attrs = self._product_attrs(product_id) if product_id else {}
        if not attrs:
            for prod in self._repo.products:
                if (prod.get("sku") or "").upper() == sku:
                    attrs = {a["name"]: a["value"] for a in prod.get("attributes", [])}
                    break

        spirit_slugs = attrs.get("pa_spirits", [])
        mixer_slugs  = attrs.get("pa_mixers",  [])
        spirit_cl    = self._repo.parse_cl_to_ml(attrs.get("pa_spirits-amount", ["4"])) / 10
        mixer_cl     = self._repo.parse_cl_to_ml(attrs.get("pa_mixers-amount", ["21"])) / 10

        spirits = self._build_spirits([(s, spirit_cl) for s in spirit_slugs])
        mixers  = self._build_mixers( [(m, mixer_cl)  for m in mixer_slugs])
        return DrinkSpec(name=name, spirits=spirits, mixers=mixers)

    def _product_attrs(self, product_id: int) -> dict:
        prod = self._repo.product_by_id(product_id)
        if not prod:
            return {}
        return {a["name"]: a["value"] for a in prod.get("attributes", [])}

    def _build_spirits(self, entries: list[tuple[str, float]]) -> list[dict]:
        measure_cl = self._repo.spirit_measure_cl()
        result = []
        for slug, cl in entries:
            slot      = self._repo.ingredient_to_slot(slug)
            pours     = max(1, round(cl / measure_cl))
            viscosity = self._repo.spirit_viscosity(slug)
            result.append({"ingredient": slug, "slot": slot,
                           "pours": pours, "viscosity": viscosity})
        return result

    def _build_mixers(self, entries: list[tuple[str, float]]) -> list[dict]:
        result = []
        for slug, cl in entries:
            slot      = self._repo.ingredient_to_slot(slug)
            viscosity = self._repo.mixer_viscosity(slug)
            ml        = cl * 10 * viscosity
            result.append({"ingredient": slug, "slot": slot, "ml": ml})
        return result
