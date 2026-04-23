"""
BarbotRepository – single data-access layer.

Encapsulates WooClient (remote API) and the three local config caches
(StoreConfig, AttributesConfig, BarbotConfig).  All domain classes
(InventoryManager, DrinkResolver, BarbotFSM) depend on this one object
instead of importing WooClient, StoreConfig, and AttributesConfig
separately.

Responsibilities:
  - Fetch and cache WooCommerce products & attributes
  - Normalise slot mapping (display names → slugs)
  - Expose order polling, order completion, heartbeat
  - Expose viscosity persistence
  - Provide read access to slot config, store cache, and bottle properties
"""

import logging
from datetime import datetime, timezone

from config import BarbotConfig, AttributesConfig, SPIRIT_SLOTS, MIXER_SLOTS
from store import StoreConfig
from woo_client import WooClient

log = logging.getLogger("REPO")


class BarbotRepository:
    """
    Facade over WooClient + StoreConfig + AttributesConfig + BarbotConfig.
    Injected into every class that needs data access so WooClient never
    leaks into FSM, inventory, or resolver logic directly.

    SCALABILITY NOTE: This class currently handles network I/O (WooClient),
    file persistence (StoreConfig, AttributesConfig, BarbotConfig), and
    some business logic (slot/viscosity). If this grows beyond ~700 lines,
    consider splitting into HardwareMapper (slot/viscosity) and
    CloudSyncService (Woo integration), with the Repository as facade.
    """

    def __init__(
        self,
        woo:        WooClient,
        store:      StoreConfig,
        attributes: AttributesConfig,
        config:     BarbotConfig,
    ):
        self._woo        = woo
        self._store      = store
        self._attributes = attributes
        self._config     = config

    # ─────────────────────────────────────────────────────────────────────────
    # Slot / config access
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def config(self) -> BarbotConfig:
        return self._config

    @property
    def slot_mapping(self) -> dict[str, str]:
        return self._config.slot_mapping

    @property
    def empty_slots(self) -> set[str]:
        return self._config.empty_slots

    @property
    def poll_interval(self) -> int:
        return self._config.poll_interval

    def slot_ingredient(self, slot: str) -> str | None:
        return self._config.slot_ingredient(slot)

    def ingredient_to_slot(self, ingredient: str) -> str | None:
        return self._config.ingredient_to_slot.get(ingredient)

    def set_slot(self, slot: str, ingredient: str) -> None:
        log.info("[REPO] Set slot %s → %s", slot, ingredient)
        self._config.set_slot(slot, ingredient)
        self._config.save()

    def clear_slot(self, slot: str) -> None:
        log.info("[REPO] Clear slot %s", slot)
        self._config.clear_slot(slot)
        self._config.save()

    # ─────────────────────────────────────────────────────────────────────────
    # Store cache access
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def last_fetched(self) -> str | None:
        return self._store.last_fetched

    @property
    def products(self) -> list[dict]:
        return self._store.products

    @property
    def available_spirits(self) -> list[str]:
        return self._store.available_spirits

    @property
    def available_mixers(self) -> list[str]:
        return self._store.available_mixers

    def product_by_id(self, product_id: int) -> dict | None:
        return self._store.product_by_id(product_id)

    def get_preset_recipes(self) -> dict:
        return self._store.get_preset_recipes(self._attributes.attribute_slugs)

    def get_diy_volumes(self) -> dict:
        return self._store.get_diy_volumes(self._attributes.attribute_slugs)

    def parse_cl_to_ml(self, val) -> float:
        return StoreConfig.parse_cl_to_ml(val)

    # ─────────────────────────────────────────────────────────────────────────
    # Attribute / bottle-property access
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def attribute_ids(self) -> dict[str, int]:
        return self._attributes.attribute_ids

    @property
    def attribute_slugs(self) -> dict[str, str]:
        return self._attributes.attribute_slugs

    @property
    def term_slugs(self) -> dict[str, dict[str, str]]:
        return self._attributes.term_slugs

    @property
    def bottle_properties(self) -> dict:
        return self._attributes.bottle_properties

    def spirit_viscosity(self, slug: str) -> float:
        return (self._attributes.bottle_properties
                .get("pa_spirits", {}).get(slug, {}).get("viscosity", 1.0))

    def mixer_viscosity(self, slug: str) -> float:
        return (self._attributes.bottle_properties
                .get("pa_mixers", {}).get(slug, {}).get("viscosity", 1.0))

    def spirit_measure_cl(self) -> float:
        from config import load_json, SLOTS_CONFIG_PATH
        data = load_json(SLOTS_CONFIG_PATH)
        return float(data.get("spirit_measures_cl", 2))

    # ─────────────────────────────────────────────────────────────────────────
    # Slug ↔ display-name helpers
    # ─────────────────────────────────────────────────────────────────────────

    def slug_to_name(self, slug: str) -> str:
        spirits = {s: n for n, s in self.term_slugs.get('pa_spirits', {}).items()}
        mixers  = {s: n for n, s in self.term_slugs.get('pa_mixers',  {}).items()}
        return spirits.get(slug) or mixers.get(slug) or slug

    def attr_slug(self, display_name: str) -> str:
        return self._attributes.attribute_slugs.get(display_name, display_name.lower())

    # ─────────────────────────────────────────────────────────────────────────
    # Remote fetch operations
    # ─────────────────────────────────────────────────────────────────────────

    def fetch_all_store_data(self) -> None:
        """Fetch attributes + products from WooCommerce and update local caches."""
        log.info("[REPO] Fetching WooCommerce attributes…")
        self._fetch_attributes()
        log.info("[REPO] Fetching WooCommerce products…")
        self._store.fetch(self._woo, self._attributes.term_slugs)
        self._normalize_slots()
        log.info("[REPO] Store data refreshed – %d products", len(self._store.products))

    def fetch_orders(self) -> list[dict]:
        """Return all orders with status 'processing', sorted by id."""
        log.info("[REPO] Fetching pending orders")
        orders = self._woo.fetch_all("orders", {"status": "processing"})
        return sorted(orders, key=lambda o: o["id"])

    def complete_order(self, order_id: int) -> bool:
        log.info("[REPO] Marking order #%d completed", order_id)
        return self._woo.update_order_status(order_id, "completed")

    def send_heartbeat(self) -> None:
        self._woo.send_heartbeat()

    # ─────────────────────────────────────────────────────────────────────────
    # WooCommerce batch stock updates (used by InventoryManager)
    # ─────────────────────────────────────────────────────────────────────────

    def fetch_all(self, endpoint: str, params: dict | None = None) -> list[dict]:
        return self._woo.fetch_all(endpoint, params)

    def batch_update_products(self, updates: list[dict]) -> bool:
        return self._woo.batch_update_products(updates)

    def batch_update_variations(self, parent_id: int, updates: list[dict]) -> bool:
        return self._woo.batch_update_variations(parent_id, updates)

    def update_term_viscosity(self, attr_id: int, term_id: int, value: float) -> bool:
        log.info("[REPO] Updating viscosity – attr_id=%d  term_id=%d  value=%.2f",
                 attr_id, term_id, value)
        ok = self._woo.update_term_viscosity(attr_id, term_id, value)
        if ok:
            log.info("[REPO] Viscosity updated remotely")
        else:
            log.warning("[REPO] Remote viscosity update failed")
        return ok

    def save_viscosity_local(self, attr_slug: str, term_slug: str, value: float) -> None:
        bp = self._attributes.bottle_properties.get(attr_slug, {})
        if term_slug in bp:
            bp[term_slug]['viscosity'] = value
        self._attributes.save()
        log.info("[REPO] Viscosity saved locally: attr=%s  term=%s  val=%.2f",
                 attr_slug, term_slug, value)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_attributes(self) -> None:
        try:
            attributes = self._woo.fetch_all('products/attributes')
        except Exception as exc:
            log.warning("[REPO] Attribute fetch failed: %s", exc)
            return

        attr_ids, attr_slugs, term_slugs, bottle_props = {}, {}, {}, {}
        for attr in attributes:
            aid  = attr.get('id')
            anam = attr.get('name')
            aslg = attr.get('slug')
            if not all((aid, anam, aslg)):
                continue
            attr_ids[aslg]   = aid
            attr_slugs[anam] = aslg
            try:
                terms = self._woo.fetch_all(f'products/attributes/{aid}/terms')
            except Exception:
                continue
            term_slugs[aslg]   = {}
            bottle_props[aslg] = {}
            for term in terms:
                tid  = term.get('id')
                tnam = term.get('name')
                tslg = term.get('slug')
                if not all((tid, tnam, tslg)):
                    continue
                term_slugs[aslg][tnam] = tslg
                bp = term.get('bottle_properties', {})
                bottle_props[aslg][tslg] = {
                    'id':          tid,
                    'name':        tnam,
                    'bottle_size': float(bp.get('bottle_size', 70)),
                    'viscosity':   float(bp.get('viscosity',   1)),
                }
        self._attributes.update_attributes(attr_ids, attr_slugs, term_slugs, bottle_props)
        self._attributes.save()

    def _normalize_slots(self) -> None:
        name_to_slug = {
            name: slug
            for terms in self._attributes.term_slugs.values()
            for name, slug in terms.items()
        }
        if not name_to_slug:
            return
        if any(ing in name_to_slug for ing in self._config.slot_mapping.values()):
            for slot, ing in list(self._config.slot_mapping.items()):
                if ing in name_to_slug:
                    self._config.slot_mapping[slot] = name_to_slug[ing]
            self._config.save()
            log.info("[REPO] Slot mapping normalised to slugs")
