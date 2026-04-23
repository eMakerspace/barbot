"""
HeartbeatService – sends periodic keep-alive pings to the WooCommerce endpoint.

Order fetching, resolution, and completion are all handled by BarbotFSM
via BarbotRepository.  This class has exactly one responsibility.
"""

import logging
import threading

from repository import BarbotRepository

log = logging.getLogger("HEARTBEAT")

_INTERVAL = 15  # seconds between pings


class HeartbeatService:

    def __init__(self, repo: BarbotRepository):
        self._repo  = repo
        self._stop  = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        log.info("[HEARTBEAT] Starting (interval=%ds)", _INTERVAL)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="Heartbeat")
        self._thread.start()

    def stop(self) -> None:
        log.info("[HEARTBEAT] Stopping")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=_INTERVAL + 5)

    def _run(self) -> None:
        while not self._stop.wait(_INTERVAL):
            try:
                self._repo.send_heartbeat()
                log.debug("[HEARTBEAT] Ping sent")
            except Exception as exc:
                log.warning("[HEARTBEAT] Ping failed: %s", exc)
