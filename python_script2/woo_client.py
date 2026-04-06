"""WooCommerce API wrapper with pagination and batch helpers."""

import time

import requests
from woocommerce import API as WooAPI


class WooClient:
    """Thin wrapper around the WooCommerce REST API."""

    def __init__(self, env: dict[str, str]):
        self._env = env
        self._api = WooAPI(
            url=env["WOOCOMMERCE_URL"],
            consumer_key=env["WOOCOMMERCE_KEY"],
            consumer_secret=env["WOOCOMMERCE_SECRET"],
            version="wc/v3",
            timeout=15,
        )

    def get(self, endpoint: str, params: dict | None = None):
        return self._api.get(endpoint, params=params or {})

    def put(self, endpoint: str, data: dict):
        return self._api.put(endpoint, data)

    def post(self, endpoint: str, data: dict):
        return self._api.post(endpoint, data)

    def fetch_all(self, endpoint: str, params: dict | None = None) -> list[dict]:
        """Paginated GET returning all items."""
        results = []
        page = 1
        base = params or {}
        while True:
            resp = self._api.get(endpoint, params={**base, "per_page": 100, "page": page})
            if resp.status_code != 200:
                print(f"  [API] GET {endpoint} failed (HTTP {resp.status_code})")
                break
            batch = resp.json()
            if not batch:
                break
            results.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return results

    def batch_update_products(self, updates: list[dict]) -> bool:
        if not updates:
            return True
        resp = self._api.post("products/batch", {"update": updates})
        if resp.status_code == 200:
            return True
        print(f"  [API] Batch product update failed (HTTP {resp.status_code}).")
        return False

    def batch_update_variations(self, parent_id: int, updates: list[dict]) -> bool:
        if not updates:
            return True
        resp = self._api.post(f"products/{parent_id}/variations/batch", {"update": updates})
        if resp.status_code == 200:
            return True
        print(f"  [API] Batch variation update for #{parent_id} failed (HTTP {resp.status_code}).")
        return False

    def update_order_status(self, order_id: int, status: str, retries: int = 3) -> bool:
        for attempt in range(1, retries + 1):
            try:
                resp = self._api.put(f"orders/{order_id}", {"status": status})
                if resp.status_code == 200:
                    return True
                print(f"  [API] Unexpected status {resp.status_code}, attempt {attempt}/{retries}")
            except Exception as e:
                print(f"  [API] Error updating order #{order_id} (attempt {attempt}): {e}")
            time.sleep(2)
        return False

    def send_heartbeat(self):
        url = self._env["WOOCOMMERCE_URL"].rstrip("/") + "/wp-json/barmachine/v1/ping"
        token = self._env.get("HEARTBEAT_TOKEN", "")
        try:
            requests.post(url, data={"secret": token}, timeout=5)
        except Exception as e:
            print(f"  [HEARTBEAT] Failed: {e}")

    def update_term_viscosity(self, attribute_id: int, term_id: int, viscosity: float) -> bool:
        """Update the viscosity property of a specific attribute term."""
        endpoint = f"products/attributes/{attribute_id}/terms/{term_id}"
        payload = {"viscosity": viscosity}

        try:
            response = self._api.put(endpoint, payload)
            if response.status_code in [200, 201]:
                print(f"  [API] Successfully updated Term {term_id} viscosity to {viscosity}.")
                return True
            else:
                print(f"  [API] Failed to update Term {term_id} (HTTP {response.status_code})")
                return False
        except Exception as e:
            print(f"  [API] Error updating term {term_id}: {e}")
            return False
