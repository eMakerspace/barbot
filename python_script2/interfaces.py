"""
Abstract hardware interfaces.

BarbotFSM depends only on these ABCs — never on concrete dummy or real
implementations.  Swap DummyMachine for RpiMachine without touching the FSM.
"""

from abc import ABC, abstractmethod


class AbstractLED(ABC):
    """LED strip firmware interface."""

    @abstractmethod
    def set(self, mode: str, order_num: int | None = None) -> None:
        """
        Set the LED animation mode. Implementations must be idempotent (no-op
        when called with the same mode+order_num twice in a row).

        Modes:
          idle        – idle animations
          cup_missing – waiting for cup (orange blink)
          moving      – moving to slot
          pouring     – pouring spirit
          mixing      – dispensing mixer (order_num breathes on 7-seg)
          finished    – drink done, waiting for cup removal
                        (order_num blinks on 7-seg)
          warning     – mixer stall / bottle empty
          emergency   – emergency stop
        """


class AbstractMachine(ABC):
    """
    Main motion firmware interface.

    Encapsulates the X-axis stepper, spirit optic, peristaltic mixer pump,
    cup sensor, AND the load cell.  The scale is internal to the machine
    firmware — callers request a target weight and the firmware handles
    closed-loop control internally.
    """

    # ── Cup sensor ───────────────────────────────────────────────────────────

    @abstractmethod
    def cup_present(self) -> bool:
        """Return True if a cup is detected on the platform."""

    # ── Motion ───────────────────────────────────────────────────────────────

    @abstractmethod
    def homing(self) -> None:
        """Run the full homing sequence and move to idle position."""

    @abstractmethod
    def move_to(self, target_steps: int) -> None:
        """Move X axis to an absolute step position."""

    @abstractmethod
    def move_to_idle(self) -> None:
        """Move X axis to the configured idle position."""

    @abstractmethod
    def move_to_slot(self, slot: str) -> None:
        """Move X axis to the position configured for the named slot."""

    @property
    @abstractmethod
    def x_position(self) -> int:
        """Current X axis position in steps."""

    @property
    @abstractmethod
    def x_max(self) -> int:
        """Maximum X axis travel in steps."""

    # ── Dispensing ───────────────────────────────────────────────────────────

    @abstractmethod
    def pour_spirit(self, slot: str, pours: int, viscosity: float = 1.0) -> None:
        """Trigger the spirit optic `pours` times at the given slot."""

    @abstractmethod
    def pour_mixer(self, slot: str, target_g: float) -> None:
        """
        Run the mixer pump until the internal scale reads `target_g` net grams.
        Raises MixerStall if the weight stops changing (bottle empty / pipe blocked).
        """

    @abstractmethod
    def stop_pump(self) -> None:
        """Immediately stop the mixer pump (emergency or stall recovery)."""

    # ── Scale (exposed for maintenance reads only) ────────────────────────────

    @abstractmethod
    def tare_scale(self) -> None:
        """Zero the load cell."""

    @abstractmethod
    def read_weight(self) -> float:
        """Return the current net weight in grams (after tare)."""
