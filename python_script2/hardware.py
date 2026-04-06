"""Hardware interface – dummy implementation for development."""

import time

from config import BarbotConfig

# Configurable movement range for the X axis (steps)
X_MOVE_MIN =    0
X_MOVE_MAX = 6000


class HardwareInterface:
    """Abstraction for physical machine control. Subclass for real GPIO."""

    def __init__(self, config: BarbotConfig):
        self.config = config
        self.x_position: int = 0

    def homing(self):
        """Initialize hardware and home all axes (dummy implementation)."""
        time.sleep(0.5)

    def dispense(self, slot: str, ml: float):
        time.sleep(0.3)

    def display_order_id(self, short_id: int):
        pass

    def signal_done(self):
        pass

    def move_x(self, position: int) -> str:
        """Move X axis to absolute position (dummy: simulates travel time)."""
        time.sleep(0.5)
        self.x_position = position
        return f'Moved to {position}'

    def clean_mixer(self, slot: str, grams: int) -> str:
        """Pour through a mixer slot by weight for cleaning (dummy)."""
        time.sleep(0.5)
        return f'{slot} cleaned {grams}g'

    def clean_spirit(self, slot: str, count: int) -> str:
        """Pour through a spirit slot N times for cleaning (dummy)."""
        for _ in range(count):
            time.sleep(0.1)
        return f'{slot} cleaned x{count}'

    def check_slot_sensor(self, slot: str) -> bool:
        return slot not in self.config.empty_slots
