"""Crash-safe order progress tracking.

Writes progress to disk after every completed drink so that on reboot
only the remaining drinks of an interrupted order are made.
"""

from pathlib import Path

from config import load_json, save_json, BASE_DIR

PROGRESS_PATH = BASE_DIR / "config" / "order_progress.json"


class OrderProgress:
    """Persists which drink index was last completed for a given order."""

    def __init__(self, path: Path = PROGRESS_PATH):
        self._path = path
        self._order_id: int | None = None
        self._total: int = 0
        self._completed: int = 0

    def start(self, order_id: int, total: int):
        """Record that we are beginning a new order."""
        self._order_id = order_id
        self._total = total
        self._completed = 0
        self._save()

    def resume_from_disk(self, order_id: int, total: int) -> int:
        """
        Load saved progress for this order_id.
        Returns the number of drinks already completed (i.e. start from this index).
        Returns 0 if no matching progress exists.
        """
        data = self._load()
        if data.get("order_id") == order_id and data.get("total") == total:
            completed = data.get("completed", 0)
            self._order_id = order_id
            self._total = total
            self._completed = completed
            if completed > 0:
                print(f"[PROGRESS] Resuming order #{order_id}: "
                      f"{completed}/{total} drinks already made, skipping them.")
            return completed
        return 0

    def drink_done(self):
        """Call after each drink is successfully dispensed."""
        self._completed += 1
        self._save()

    def clear(self):
        """Call after the order is fully completed."""
        if self._path.exists():
            self._path.unlink()
        self._order_id = None
        self._total = 0
        self._completed = 0

    def _save(self):
        save_json({
            "order_id": self._order_id,
            "total": self._total,
            "completed": self._completed,
        }, self._path)

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return load_json(self._path)
        except Exception:
            return {}
