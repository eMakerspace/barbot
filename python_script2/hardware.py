"""Hardware interface – dummy implementation for development."""

import time

from config import BarbotConfig


class HardwareInterface:
    """Abstraction for physical machine control. Subclass for real GPIO."""

    def __init__(self, config: BarbotConfig):
        self.config = config

    def homing(self):
        """Initialize hardware and home all axes (dummy implementation)."""
        print("  [HARDWARE] Running homing sequence...")
        time.sleep(0.5)
        print("  [HARDWARE] Homing complete!")

    def dispense(self, slot: str, ml: float):
        ingredient = self.config.slot_ingredient(slot) or "???"
        print(f"  [HARDWARE] Dispensing {ml:.0f} ml from {slot} ({ingredient})")
        time.sleep(0.3)

    def display_order_id(self, short_id: int):
        print(f"  [DISPLAY] Showing order #{short_id:02d}")

    def signal_done(self):
        print("  [HARDWARE] *** DRINK READY ***")

    def check_slot_sensor(self, slot: str) -> bool:
        return slot not in self.config.empty_slots
