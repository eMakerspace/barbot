"""
Dummy hardware drivers – three concrete implementations of the ABCs in interfaces.py.

Firmware boundaries:
  DummyLED     – LED strip animation controller (separate MCU / serial)
  DummyMachine – Main motion firmware: X-axis stepper + spirit optic +
                 peristaltic pump + cup sensor + load cell (all one embedded controller)

DummyScale is an internal implementation detail of DummyMachine.
Nothing outside this module holds a reference to it.
"""

import logging
import threading
import time

from interfaces import AbstractLED, AbstractMachine

log = logging.getLogger("HW_DUMMY")


class MixerStall(Exception):
    """Raised by pour_mixer when weight stops changing (bottle empty / pipe blocked)."""


# ─────────────────────────────────────────────────────────────────────────────
# LED firmware
# ─────────────────────────────────────────────────────────────────────────────

class DummyLED(AbstractLED):
    """Simulates a separate LED-strip animation controller."""

    _MODES = {
        "idle":        ("#E8E8E8", "grey pulse – Idle"),
        "cup_missing": ("#FFB347", "orange blink – Cup Missing"),
        "pouring":     ("#ADD8E6", "blue chase – Pouring"),
        "finished":    ("#98FB98", "green fade – Finished"),
        "warning":     ("#FFFF99", "yellow flash – Warning"),
        "emergency":   ("#FF6961", "red strobe – Emergency"),
    }

    def __init__(self):
        self._mode = "idle"
        log.info("[LED] Initialised – mode: idle")

    def set(self, mode: str) -> None:
        if mode == self._mode:
            return
        if mode not in self._MODES:
            log.warning("[LED] Unknown mode '%s' – ignored", mode)
            return
        colour, desc = self._MODES[mode]
        log.info("[LED] ◈ %s  colour=%s  (%s)", mode.upper(), colour, desc)
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode


# ─────────────────────────────────────────────────────────────────────────────
# Internal scale simulation (private to DummyMachine)
# ─────────────────────────────────────────────────────────────────────────────

class _SimScale:
    """Internal load-cell simulator, owned exclusively by DummyMachine."""

    def __init__(self):
        self._tare   = 0.0
        self._raw    = 0.0
        self._rate   = 0.0
        self._stop   = threading.Event()
        self._thread: threading.Thread | None = None

    def tare(self) -> None:
        log.info("[SCALE] Taring at raw=%.2f g", self._raw)
        self._tare = self._raw

    def read(self) -> float:
        return max(0.0, self._raw - self._tare)

    def set_cup(self, weight_g: float) -> None:
        self._raw = weight_g

    def remove_cup(self) -> None:
        self._raw = 0.0

    def start_fill(self, rate_g_per_s: float = 8.0) -> None:
        self._rate = rate_g_per_s
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop_fill(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        interval = 0.05
        while not self._stop.wait(interval):
            self._raw += self._rate * interval


# ─────────────────────────────────────────────────────────────────────────────
# Main machine firmware
# ─────────────────────────────────────────────────────────────────────────────

class DummyMachine(AbstractMachine):
    """
    Simulates the main machine firmware.

    Owns the scale internally – callers request a target weight via
    pour_mixer() and this class handles the closed-loop fill control.
    No external object holds a scale reference.

    Thread-safe: all hardware access is guarded by _hw_lock to prevent
    race conditions between the FSM main thread and CupCheckThread.
    """

    _STEPS_PER_S = 3000

    def __init__(
        self,
        x_max: int = 6000,
        x_idle: int = 3000,
        pour_duration_ms: int = 1500,
        settle_duration_ms: int = 500,
        slot_positions: dict | None = None,
    ):
        self._x_max             = x_max
        self._x_idle            = x_idle
        self._x_position        = 0
        self._pour_ms           = pour_duration_ms
        self._settle_ms         = settle_duration_ms
        self._slot_positions: dict[str, int] = slot_positions or {
            "Slot_1": 500,  "Slot_2": 1000, "Slot_3": 1500, "Slot_4": 2000,
            "Slot_5": 2500, "Slot_6": 3000, "Slot_7": 3500, "Slot_8": 4000,
            "Slot_A": 4500, "Slot_B": 5000, "Slot_C": 5500, "Slot_D": 6000,
        }
        self._cup               = False
        self._scale             = _SimScale()
        self._hw_lock           = threading.Lock()
        log.info("[MACHINE] Initialised – x_max=%d  x_idle=%d  pour=%dms  settle=%dms",
                 x_max, x_idle, pour_duration_ms, settle_duration_ms)

    # ── Cup sensor ───────────────────────────────────────────────────────────

    def cup_present(self) -> bool:
        with self._hw_lock:
            log.debug("[MACHINE] Cup sensor: %s", "PRESENT" if self._cup else "ABSENT")
            return self._cup

    def simulate_cup_placed(self, cup_weight_g: float = 180.0) -> None:
        with self._hw_lock:
            log.info("[MACHINE] [SIM] Cup placed (%.1f g)", cup_weight_g)
            self._cup = True
            self._scale.set_cup(cup_weight_g)

    def simulate_cup_removed(self) -> None:
        with self._hw_lock:
            log.info("[MACHINE] [SIM] Cup removed")
            self._cup = False
            self._scale.remove_cup()

    # ── Motion ───────────────────────────────────────────────────────────────

    def homing(self) -> None:
        with self._hw_lock:
            log.info("[MACHINE] ── Homing started ──")
            travel_s = self._x_position / self._STEPS_PER_S
            time.sleep(min(travel_s, 0.4))
            self._x_position = 0
            log.info("[MACHINE] Endstop hit – zeroed")
        self.move_to(self._x_idle)
        with self._hw_lock:
            log.info("[MACHINE] ── Homing complete – pos=%d ──", self._x_position)

    def move_to(self, target_steps: int) -> None:
        with self._hw_lock:
            target = max(0, min(self._x_max, target_steps))
            dist   = abs(target - self._x_position)
            travel = dist / self._STEPS_PER_S
            log.info("[MACHINE] Move X: %d → %d  (Δ%d steps  %.2fs)",
                     self._x_position, target, dist, travel)
            time.sleep(min(travel, 0.3))
            self._x_position = target
            log.info("[MACHINE] Arrived at %d", self._x_position)

    def move_to_idle(self) -> None:
        log.info("[MACHINE] Moving to idle (%d)", self._x_idle)
        self.move_to(self._x_idle)

    def move_to_slot(self, slot: str) -> None:
        pos = self._slot_positions.get(slot)
        if pos is None:
            raise ValueError(f"No position configured for slot '{slot}'")
        log.info("[MACHINE] Moving to slot %s (pos=%d)", slot, pos)
        self.move_to(pos)

    @property
    def x_position(self) -> int:
        with self._hw_lock:
            return self._x_position

    @property
    def x_max(self) -> int:
        return self._x_max

    # ── Dispensing ───────────────────────────────────────────────────────────

    def pour_spirit(self, slot: str, pours: int, viscosity: float = 1.0) -> None:
        with self._hw_lock:
            duration_ms = self._pour_ms * viscosity
            log.info("[MACHINE] Spirit pour – slot=%s  pours=%d  viscosity=%.2f  %.0fms/pour",
                     slot, pours, viscosity, duration_ms)
            for i in range(1, pours + 1):
                log.info("[MACHINE]   Pour %d/%d – opening solenoid %.0fms…", i, pours, duration_ms)
                time.sleep(duration_ms / 1000)
                log.info("[MACHINE]   Pour %d/%d – settling %dms", i, pours, self._settle_ms)
                time.sleep(self._settle_ms / 1000)
            log.info("[MACHINE] Spirit pour complete – slot=%s", slot)

    def pour_mixer(self, slot: str, target_g: float) -> None:
        with self._hw_lock:
            log.info("[MACHINE] Mixer pour – slot=%s  target=%.1fg", slot, target_g)
            start_g   = self._scale.read()
            remaining = target_g - start_g
            if remaining <= 0:
                log.info("[MACHINE] Already at target weight – skipping pump")
                return

            log.info("[MACHINE] Starting pump – need %.1fg more (current=%.1fg)",
                     remaining, start_g)
            self._scale.start_fill(rate_g_per_s=8.0)

            poll     = 0.2
            timeout  = remaining / 8.0 + 10
            deadline = time.monotonic() + timeout
            last_g   = start_g
            stall_t: float | None = None

            try:
                while True:
                    time.sleep(poll)
                    cur = self._scale.read()
                    log.debug("[MACHINE] Pump weight %.2fg / %.1fg", cur, target_g)

                    if cur >= target_g:
                        log.info("[MACHINE] Target reached – stopping pump (final=%.2fg)", cur)
                        return

                    if abs(cur - last_g) < 0.5:
                        if stall_t is None:
                            stall_t = time.monotonic()
                        elif time.monotonic() - stall_t > 3.0:
                            log.warning("[MACHINE] STALL – weight stuck at %.2fg", cur)
                            raise MixerStall(
                                f"Pump stalled at {cur:.1f}g (target {target_g:.1f}g)")
                    else:
                        stall_t = None
                    last_g = cur

                    if time.monotonic() > deadline:
                        log.error("[MACHINE] Pump timeout")
                        raise MixerStall("Pump timeout")
            finally:
                self._scale.stop_fill()

    def stop_pump(self) -> None:
        with self._hw_lock:
            log.warning("[MACHINE] EMERGENCY – pump stopped")
            self._scale.stop_fill()

    # ── Scale (maintenance access only) ──────────────────────────────────────

    def tare_scale(self) -> None:
        with self._hw_lock:
            log.info("[MACHINE] Tare scale")
            self._scale.tare()

    def read_weight(self) -> float:
        with self._hw_lock:
            g = self._scale.read()
            log.info("[MACHINE] Scale read: %.2fg", g)
            return g
