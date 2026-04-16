"""WooCommerce API wrapper with pagination and batch helpers."""

import time

import requests
from woocommerce import API as WooAPI
from logger import log_debug, log_info, log_warn, log_error


class WooClient:
    """Thin wrapper around the WooCommerce REST API."""

    def __init__(self, env: dict[str, str]):
        self._env = env
        log_info("WOO", f"Initializing WooCommerce API: {env.get('WOOCOMMERCE_URL')}")
        try:
            self._api = WooAPI(
                url=env["WOOCOMMERCE_URL"],
                consumer_key=env["WOOCOMMERCE_KEY"],
                consumer_secret=env["WOOCOMMERCE_SECRET"],
                version="wc/v3",
                timeout=15,
            )
            log_info("WOO", "WooCommerce API initialized successfully")
        except KeyError as e:
            log_error("WOO", f"Missing environment variable: {e}")
            raise
        except Exception as e:
            log_error("WOO", f"Failed to initialize WooAPI: {e}")
            raise

    def get(self, endpoint: str, params: dict | None = None):
        try:
            log_debug("WOO", f"GET {endpoint} with params={params}")
            resp = self._api.get(endpoint, params=params or {})
            log_debug("WOO", f"GET {endpoint} → HTTP {resp.status_code}")
            return resp
        except Exception as e:
            log_error("WOO", f"GET {endpoint} failed: {e}")
            raise

    def put(self, endpoint: str, data: dict):
        try:
            log_debug("WOO", f"PUT {endpoint}")
            resp = self._api.put(endpoint, data)
            log_debug("WOO", f"PUT {endpoint} → HTTP {resp.status_code}")
            return resp
        except Exception as e:
            log_error("WOO", f"PUT {endpoint} failed: {e}")
            raise

    def post(self, endpoint: str, data: dict):
        try:
            log_debug("WOO", f"POST {endpoint}")
            resp = self._api.post(endpoint, data)
            log_debug("WOO", f"POST {endpoint} → HTTP {resp.status_code}")
            return resp
        except Exception as e:
            log_error("WOO", f"POST {endpoint} failed: {e}")
            raise

    def fetch_all(self, endpoint: str, params: dict | None = None) -> list[dict]:
        """Paginated GET returning all items."""
        log_info("WOO", f"Fetching all from {endpoint}...")
        results = []
        page = 1
        base = params or {}
        while True:
            try:
                log_debug("WOO", f"Fetching page {page} from {endpoint}")
                resp = self._api.get(endpoint, params={**base, "per_page": 100, "page": page})
                if resp.status_code != 200:
                    log_warn("WOO", f"fetch_all {endpoint} page {page}: HTTP {resp.status_code}")
                    break
                batch = resp.json()
                if not batch:
                    log_debug("WOO", f"fetch_all {endpoint} page {page}: empty batch")
                    break
                results.extend(batch)
                log_debug("WOO", f"fetch_all {endpoint} page {page}: got {len(batch)} items, total={len(results)}")
                if len(batch) < 100:
                    break
                page += 1
            except Exception as e:
                log_error("WOO", f"fetch_all {endpoint} page {page} failed: {e}")
                break
        log_info("WOO", f"fetch_all {endpoint}: complete with {len(results)} total items")
        return results

    def batch_update_products(self, updates: list[dict]) -> bool:
        if not updates:
            return True
        try:
            log_info("WOO", f"Batch updating {len(updates)} products...")
            resp = self._api.post("products/batch", {"update": updates})
            success = resp.status_code == 200
            if success:
                log_info("WOO", f"Batch product update successful")
            else:
                log_warn("WOO", f"Batch product update failed: HTTP {resp.status_code}")
            return success
        except Exception as e:
            log_error("WOO", f"Batch product update failed: {e}")
            return False

    def batch_update_variations(self, parent_id: int, updates: list[dict]) -> bool:
        if not updates:
            return True
        try:
            log_info("WOO", f"Batch updating {len(updates)} variations for product {parent_id}...")
            resp = self._api.post(f"products/{parent_id}/variations/batch", {"update": updates})
            success = resp.status_code == 200
            if success:
                log_info("WOO", f"Batch variation update successful")
            else:
                log_warn("WOO", f"Batch variation update failed: HTTP {resp.status_code}")
            return success
        except Exception as e:
            log_error("WOO", f"Batch variation update failed: {e}")
            return False

    def update_order_status(self, order_id: int, status: str, retries: int = 3) -> bool:
        for attempt in range(1, retries + 1):
            try:
                log_debug("WOO", f"Updating order {order_id} status to '{status}' (attempt {attempt}/{retries})")
                resp = self._api.put(f"orders/{order_id}", {"status": status})
                if resp.status_code == 200:
                    log_info("WOO", f"Order {order_id} status updated to '{status}'")
                    return True
                else:
                    log_warn("WOO", f"Order {order_id} update failed: HTTP {resp.status_code}")
            except Exception as e:
                log_warn("WOO", f"Order {order_id} update attempt {attempt} failed: {e}")
            time.sleep(2)
        log_error("WOO", f"Failed to update order {order_id} status after {retries} attempts")
        return False

    def send_heartbeat(self):
        url = self._env["WOOCOMMERCE_URL"].rstrip("/") + "/wp-json/barmachine/v1/ping"
        token = self._env.get("HEARTBEAT_TOKEN", "")
        try:
            log_debug("WOO", f"Sending heartbeat to {url}")
            resp = requests.post(url, data={"secret": token}, timeout=5)
            log_debug("WOO", f"Heartbeat response: HTTP {resp.status_code}")
        except Exception as e:
            log_warn("WOO", f"Heartbeat failed: {e}")

    def update_term_viscosity(self, attribute_id: int, term_id: int, viscosity: float) -> bool:
        """Update the viscosity property of a specific attribute term."""
        endpoint = f"products/attributes/{attribute_id}/terms/{term_id}"
        payload = {"viscosity": viscosity}

        try:
            log_info("WOO", f"Updating term viscosity: attr={attribute_id}, term={term_id}, value={viscosity}")
            response = self._api.put(endpoint, payload)
            success = response.status_code in [200, 201]
            if success:
                log_info("WOO", f"Term viscosity updated successfully")
            else:
                log_warn("WOO", f"Term viscosity update failed: HTTP {response.status_code}")
            return success
        except Exception as e:
            log_error("WOO", f"Term viscosity update failed: {e}")
            return False
