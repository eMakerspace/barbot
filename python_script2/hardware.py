"""Hardware interface – real GPIO implementation for Raspberry Pi."""

import time

from config import BarbotConfig, HardwareConfig, SPIRIT_SLOTS

# Module-level bounds used by lcd_menu before a HardwareInterface is instantiated.
# These match the hardware_config.json defaults and are overridden at runtime via hw_config.
X_MOVE_MIN = 0
X_MOVE_MAX = 6000

try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except ImportError:
    _GPIO_AVAILABLE = False
    print("[HW] RPi.GPIO not available – running in simulation mode")


class HardwareError(Exception):
    pass


class HardwareInterface:
    """Physical machine control: X axis, spirit optics, mixer pumps, sensors."""

    def __init__(self, config: BarbotConfig, hw_config: HardwareConfig):
        self.config = config
        self.hw = hw_config
        self.x_position: int = 0
        self._gpio_ready = False

        if _GPIO_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            self._gpio_ready = True
            print("[HW] GPIO ready.")

    def cleanup(self):
        if _GPIO_AVAILABLE and self._gpio_ready:
            GPIO.cleanup()

    # -- Cup sensor ----------------------------------------------------------

    def wait_for_cup(self):
        """Block until a cup is placed. Simulated with 1s delay (no sensor connected)."""
        print("[HW] Waiting for cup...")
        time.sleep(1)
        print("[HW] Cup detected.")

    def wait_for_cup_removal(self):
        """Block until the cup is removed. Simulated with 1s delay (no sensor connected)."""
        print("[HW] Waiting for cup to be taken...")
        time.sleep(1)
        print("[HW] Cup removed.")

    # -- Motion --------------------------------------------------------------

    def homing(self):
        """Home all axes and move to idle position."""
        print("[HW] Homing...")
        # TODO: trigger real homing sequence (endstop)
        time.sleep(0.5)
        self.x_position = 0
        self.move_x(self.hw.x_idle)
        print("[HW] Homing complete.")

    def move_x(self, position: int):
        """Move X axis to absolute step position."""
        position = max(0, min(position, self.hw.x_max))
        distance = abs(position - self.x_position)
        travel_s = distance / self.hw.x_max  # rough proportional travel time
        print(f"[HW] Move X: {self.x_position} → {position} ({distance} steps)")
        # TODO: send steps to stepper driver
        time.sleep(travel_s * 0.5 + 0.05)
        self.x_position = position

    def move_to_idle(self):
        self.move_x(self.hw.x_idle)

    def _move_to_slot(self, slot: str):
        pos = self.hw.position_for_slot(slot)
        if pos is None:
            raise HardwareError(f"No position configured for slot {slot}")
        self.move_x(pos)

    # -- Spirit dispense (optic) ---------------------------------------------

    def _trigger_optic(self, viscosity: float = 1.0):
        """Actuate the spirit optic once. Duration scales with viscosity."""
        duration_s = (self.hw.pour_duration_ms * viscosity) / 1000
        settle_s = self.hw.settle_duration_ms / 1000
        # TODO: pull GPIO line for optic solenoid / servo
        time.sleep(duration_s)
        time.sleep(settle_s)

    def dispense_spirit(self, slot: str, pours: int, viscosity: float = 1.0):
        """Move to spirit slot and trigger the optic `pours` times."""
        if pours <= 0:
            return
        self._move_to_slot(slot)
        duration_ms = self.hw.pour_duration_ms * viscosity
        print(f"[HW] Spirit slot {slot}: {pours} pour(s), viscosity={viscosity} ({duration_ms:.0f}ms/pour)")
        for i in range(pours):
            print(f"[HW]   pour {i + 1}/{pours}")
            self._trigger_optic(viscosity)

    # -- Mixer dispense (peristaltic pump + load cell) -----------------------

    def dispense_mixer(self, slot: str, ml: float):
        """Move to mixer slot and dispense the viscosity-adjusted ml value."""
        if ml <= 0:
            return
        self._move_to_slot(slot)
        print(f"[HW] Mixer slot {slot}: {ml:.1f} ml")
        # TODO: start pump, stop when load cell reaches target weight

    # -- High-level drink sequence -------------------------------------------

    def make_drink(self, spec):
        """
        Full sequence for one DrinkSpec:
          1. Check cup is present
          2. Pour spirits (move + optic × n) for each spirit
          3. Pour mixers  (move + pump)      for each mixer
          4. Return to idle
          5. Wait for cup removal
        """
        # 1. Cup check
        self.wait_for_cup()

        # 2 & 3. Spirits first, then mixers
        for s in spec.spirits:
            if s["slot"] is None:
                print(f"[HW] WARN: no slot for spirit '{s['ingredient']}' – skipping")
                continue
            self.dispense_spirit(s["slot"], s["pours"], s.get("viscosity", 1.0))

        for m in spec.mixers:
            if m["slot"] is None:
                print(f"[HW] WARN: no slot for mixer '{m['ingredient']}' – skipping")
                continue
            self.dispense_mixer(m["slot"], m["ml"])

        # 4. Return to idle
        self.move_to_idle()

        # 5. Wait for pickup
        self.wait_for_cup_removal()

    # -- Legacy compatibility stubs ------------------------------------------

    def dispense(self, slot: str, ml: float):
        """Compatibility shim: route to spirit or mixer dispense by slot type."""
        if slot in SPIRIT_SLOTS:
            self.dispense_spirit(slot, pours=1)
        else:
            self.dispense_mixer(slot, ml)

    def display_order_id(self, short_id: int):
        pass  # handled by LCD layer

    def signal_done(self):
        pass  # handled by LCD layer

    def move_x_raw(self, position: int) -> str:
        self.move_x(position)
        return f"Moved to {position}"

    def clean_mixer(self, slot: str, grams: int) -> str:
        self.dispense_mixer(slot, float(grams))
        return f"{slot} cleaned {grams}g"

    def clean_spirit(self, slot: str, count: int) -> str:
        self.dispense_spirit(slot, count)
        return f"{slot} cleaned x{count}"

    def check_slot_sensor(self, slot: str) -> bool:
        return slot not in self.config.empty_slots
