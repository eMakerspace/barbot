"""
Real hardware drivers for Barbot.

Two concrete implementations of the ABCs in interfaces.py, talking to the
actual firmware over serial:

  RealLED      → barbot-display  (neopixel_7segment firmware, 115200 baud)
                 Protocol: plain-text commands, fire-and-forget.
                 e.g. "-mv\n", "-pour\n", "-drinknum5\n"

  RealMachine  → barbotv2 hat    (esp32/barbotv2, USB-JTAG serial, 115200 baud)
                   X-axis stepper + spirit servo + 4× pump relays
                   Commands:
                     G28                       – home (finds both endstops)
                     G0 X{steps}               – move to full-step position
                     G0.1 X{0.0-1.0}           – move to fraction of full travel
                     G1 Z{angle}               – servo to angle (85=open, 180=closed)
                     G2 I{0-3} D{ms}           – run pump for ms (non-blocking)
                     G2.1 I{0-3} D{ms}         – run pump for ms (blocking)
                     G2 I{0-3} D0              – stop pump immediately
                     G5 P{slot4_steps} Q{slot5_steps} – set servo forbidden zone
                     T0 D{ms}                  – firmware-side wait
                     M0 / M0.1 / M1            – graceful/immediate stop / continue
                     M115                      – firmware identity
                   Completion signal: firmware logs "Move done\r" after each move.

               + barbot-scale    (esp32_pump/main.cpp, CP2102 serial, 115200 baud)
                   HX711 load cell + fill-loop monitor
                   Commands:
                     G3                        – read filtered weight
                     G3.1                      – tare
                     G3.2 W{g}                 – calibrate with known weight
                     G3.3 F{cpg}               – restore calibration factor
                     G3.4 N{n}                 – raw debug reads
                     G4 I{pump} W{g}           – fill monitor loop (emits [FILL_END])

pour_mixer() coordination:
  1. Move to slot.
  2. Tare scale (G3.1).
  3. Start fill monitor (G4 I1 W{g}) on scale ESP32.
  4. Start pump on hat (G2 I{pump_idx} D{big_timeout_ms}).
  5. Read scale serial until [FILL_END] arrives.
  6. Stop pump (G2 I{pump_idx} D0).
"""

import logging
import threading
import time

import serial

from config import HardwareConfig
from interfaces import AbstractLED, AbstractMachine

log = logging.getLogger("HW_REAL")

# Slot name → hat full-step position (sent via G0 X{steps})
# These match hardware_config.json slot_positions exactly.
# The hat firmware multiplies by MICROSTEPS (4) internally.

# Pump index on the hat (GPIO3-6 → index 0-3).
# Mixer slots A-D use pump indices 0-3.
_MIXER_SLOT_PUMP: dict[str, int] = {
    "Slot_A": 0,
    "Slot_B": 1,
    "Slot_C": 2,
    "Slot_D": 3,
}

# Conservative pump on-time cap; scale fill loop has its own 30 s timeout.
_PUMP_MAX_MS = 35_000

_MOVE_DONE_MARKER  = "Move done"
_HOMING_DONE_MARKER = "Homing successful"
_MOVE_TIMEOUT_S    = 30.0
_HOMING_TIMEOUT_S  = 60.0


class MixerStall(Exception):
    """Raised by pour_mixer when the fill loop reports empty/blocked/timeout."""


# ─────────────────────────────────────────────────────────────────────────────
# Serial helpers
# ─────────────────────────────────────────────────────────────────────────────

def _open_serial(port: str, baud: int, timeout: float = 0.2) -> serial.Serial:
    ser = serial.Serial(port, baud, timeout=timeout)
    time.sleep(0.3)
    ser.reset_input_buffer()
    return ser


def _send(ser: serial.Serial, cmd: str) -> None:
    line = (cmd.strip() + "\n").encode()
    log.debug("[HAT] >> %s", cmd.strip())
    ser.write(line)
    ser.flush()


def _send_scale(ser: serial.Serial, cmd: str) -> None:
    line = (cmd.strip() + "\n").encode()
    log.debug("[SCALE] >> %s", cmd.strip())
    ser.write(line)
    ser.flush()


def _wait_for_line(ser: serial.Serial, marker: str, timeout: float) -> bool:
    """Read lines from ser until one contains marker or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode("ascii", errors="replace").strip()
        if line:
            log.debug("[HAT] << %s", line)
        if marker in line:
            return True
    log.warning("[HAT] timed out waiting for '%s'", marker)
    return False


def _scale_readline(ser: serial.Serial, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = ser.readline()
        if raw:
            return raw.decode("ascii", errors="replace").strip()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# LED firmware (barbot-display, neopixel_7segment)
# ─────────────────────────────────────────────────────────────────────────────

class RealLED(AbstractLED):
    """Drives the neopixel/7-segment display controller over serial."""

    _MODE_CMD: dict[str, str] = {
        "idle":        "-i",
        "cup_missing": "-cupwait",
        "moving":      "-mv",
        "pouring":     "-pour",
        "mixing":      "-mix",
        "finished":    "-drinkready",
        "warning":     "-e ORANGE_FLASH",
        "emergency":   "-estop",
    }

    def __init__(self, port: str, baud: int = 115200):
        self._ser   = _open_serial(port, baud)
        self._lock  = threading.Lock()
        self._mode: str = "idle"
        self._order_num: int | None = None
        self._write("-i")
        log.info("[LED] Initialised on %s – mode: idle", port)

    def _write(self, cmd: str) -> None:
        with self._lock:
            try:
                self._ser.write((cmd + "\n").encode())
                self._ser.flush()
                log.debug("[LED] >> %s", cmd)
            except serial.SerialException as exc:
                log.error("[LED] serial error: %s", exc)

    def set(self, mode: str, order_num: int | None = None) -> None:
        if mode == self._mode and order_num == self._order_num:
            return
        cmd = self._MODE_CMD.get(mode)
        if cmd is None:
            log.warning("[LED] Unknown mode '%s' – ignored", mode)
            return
        self._write(cmd)
        if order_num is not None:
            self._write(f"-drinknum{max(0, min(99, order_num))}")
        self._mode      = mode
        self._order_num = order_num
        log.info("[LED] ◈ %s%s", mode.upper(),
                 f"  order_num={order_num}" if order_num is not None else "")

    @property
    def mode(self) -> str:
        return self._mode

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Main machine firmware (barbotv2 hat + barbot-scale)
# ─────────────────────────────────────────────────────────────────────────────

class RealMachine(AbstractMachine):
    """
    Drives the barbotv2 hat (X-axis stepper + spirit servo + pump relays)
    and barbot-scale (HX711 load cell + fill-loop monitor) over two serial ports.

    All public methods acquire _hw_lock to prevent races between the FSM
    thread and CupCheckThread.
    """

    def __init__(self, hw_config: HardwareConfig):
        self._hw_config = hw_config
        self._x_max     = hw_config.x_max
        self._x_idle    = hw_config.x_idle
        self._x_pos     = 0
        self._hw_lock   = threading.Lock()

        # barbotv2 uses USB JTAG serial – baud is ignored by the hardware but
        # pyserial still requires a value; 115200 is conventional.
        self._hat_ser   = _open_serial(hw_config.hat_port,   115200)
        self._scale_ser = _open_serial(hw_config.scale_port, hw_config.scale_baud)

        # Drain startup banners ("Startup...", "Barbot HAT v… running", etc.)
        time.sleep(0.5)
        self._hat_ser.reset_input_buffer()
        self._scale_ser.reset_input_buffer()

        # Tell the hat the forbidden servo zone (between Slot_4 and Slot_5)
        self._configure_forbidden_zone()

        log.info("[MACHINE] Initialised – x_max=%d  x_idle=%d  pour=%dms  settle=%dms",
                 self._x_max, self._x_idle,
                 hw_config.pour_duration_ms, hw_config.settle_duration_ms)

    def _configure_forbidden_zone(self) -> None:
        """Send G5 so the hat knows where it must not open the servo."""
        zones = self._hw_config.forbidden_servo_zones
        if not zones:
            # Fall back to Slot_4 / Slot_5 from slot_positions if no explicit zones.
            p4 = self._hw_config.slot_positions.get("Slot_4")
            p5 = self._hw_config.slot_positions.get("Slot_5")
            if p4 is not None and p5 is not None:
                _send(self._hat_ser, f"G5 P{p4} Q{p5}")
                log.info("[MACHINE] Forbidden zone sent: Slot_4=%d Slot_5=%d", p4, p5)
        else:
            z = zones[0]
            _send(self._hat_ser, f"G5 P{z[0]} Q{z[1]}")
            log.info("[MACHINE] Forbidden zone sent: P=%d Q=%d", z[0], z[1])

    # ── Cup sensor ───────────────────────────────────────────────────────────

    def cup_present(self) -> bool:
        """
        Detect cup via the hat's GPIO2 cup sensor.
        The hat logs "Cup present" / "Cup absent" from the cup_presence_monitor task.
        We use the scale as a weight-based fallback: > 50 g means a cup is present.
        """
        with self._hw_lock:
            try:
                _send_scale(self._scale_ser, "G3")
                line = _scale_readline(self._scale_ser, timeout=2.0)
                if line and line.startswith("Weight:"):
                    g = float(line.split(":")[1].replace("g", "").strip())
                    present = g > 50.0
                    log.debug("[MACHINE] Cup sensor (scale %.2fg): %s",
                              g, "PRESENT" if present else "ABSENT")
                    return present
            except Exception as exc:
                log.warning("[MACHINE] cup_present scale read error: %s", exc)
            return False

    # ── Motion ───────────────────────────────────────────────────────────────

    def homing(self) -> None:
        with self._hw_lock:
            log.info("[MACHINE] ── Homing started ──")
            _send(self._hat_ser, "G28")
            _wait_for_line(self._hat_ser, _HOMING_DONE_MARKER, _HOMING_TIMEOUT_S)
            self._x_pos = 0
            log.info("[MACHINE] Endstop hit – zeroed")
        self.move_to(self._x_idle)
        log.info("[MACHINE] ── Homing complete – pos=%d ──", self._x_pos)

    def move_to(self, target_steps: int) -> None:
        """Move to absolute full-step position (hat multiplies by MICROSTEPS=4 internally)."""
        with self._hw_lock:
            target = max(0, min(self._x_max, target_steps))
            log.info("[MACHINE] Move X: %d → %d  (Δ%d steps)", self._x_pos, target, abs(target - self._x_pos))
            _send(self._hat_ser, f"G0 X{target}")
            _wait_for_line(self._hat_ser, _MOVE_DONE_MARKER, _MOVE_TIMEOUT_S)
            self._x_pos = target
            log.info("[MACHINE] Arrived at %d", self._x_pos)

    def move_to_idle(self) -> None:
        log.info("[MACHINE] Moving to idle (%d)", self._x_idle)
        self.move_to(self._x_idle)

    def move_to_slot(self, slot: str) -> None:
        pos = self._hw_config.slot_positions.get(slot)
        if pos is None:
            raise ValueError(f"No position configured for slot '{slot}'")
        with self._hw_lock:
            log.info("[MACHINE] Moving to slot %s (pos=%d)", slot, pos)
            _send(self._hat_ser, f"G0 X{pos}")
            _wait_for_line(self._hat_ser, _MOVE_DONE_MARKER, _MOVE_TIMEOUT_S)
            self._x_pos = pos
            log.info("[MACHINE] Arrived at slot %s", slot)

    @property
    def x_position(self) -> int:
        with self._hw_lock:
            return self._x_pos

    @property
    def x_max(self) -> int:
        return self._x_max

    # ── Dispensing ───────────────────────────────────────────────────────────

    def pour_spirit(self, slot: str, pours: int, viscosity: float = 1.0) -> None:
        """
        Move to slot then actuate the servo optic `pours` times.
        Pour sequence per shot: open servo (85°) → wait pour_ms → close servo (180°) → settle.
        """
        self.move_to_slot(slot)
        pour_ms     = int(self._hw_config.pour_duration_ms * viscosity)
        settle_ms   = self._hw_config.settle_duration_ms
        open_angle  = self._hw_config.pour_angle
        close_angle = self._hw_config.close_angle

        log.info("[MACHINE] Spirit pour – slot=%s  pours=%d  viscosity=%.2f  %dms/pour",
                 slot, pours, viscosity, pour_ms)
        with self._hw_lock:
            for i in range(1, pours + 1):
                log.info("[MACHINE]   Pour %d/%d – opening servo to %d°", i, pours, open_angle)
                _send(self._hat_ser, f"G1 Z{open_angle}")
                time.sleep(pour_ms / 1000)
                log.info("[MACHINE]   Pour %d/%d – closing servo to %d°", i, pours, close_angle)
                _send(self._hat_ser, f"G1 Z{close_angle}")
                log.info("[MACHINE]   Pour %d/%d – settling %dms", i, pours, settle_ms)
                time.sleep(settle_ms / 1000)
        log.info("[MACHINE] Spirit pour complete – slot=%s", slot)

    def pour_mixer(self, slot: str, target_g: float) -> None:
        """
        Move to mixer slot then run closed-loop fill:
          1. Tare scale (G3.1).
          2. Start fill monitor on scale ESP32 (G4 I1 W{g}).
          3. Start pump on hat (G2 I{pump_idx} D{max_ms}).
          4. Read scale serial until [FILL_END].
          5. Stop pump (G2 I{pump_idx} D0).
        """
        self.move_to_slot(slot)
        pump_idx = _MIXER_SLOT_PUMP.get(slot)
        if pump_idx is None:
            raise ValueError(f"No pump index configured for mixer slot '{slot}'")

        log.info("[MACHINE] Mixer pour – slot=%s  pump=%d  target=%.1fg", slot, pump_idx, target_g)

        with self._hw_lock:
            if target_g <= 0:
                log.info("[MACHINE] Zero target – skipping pump")
                return

            # Tare before fill
            _send_scale(self._scale_ser, "G3.1")
            time.sleep(0.8)
            self._scale_ser.reset_input_buffer()

            # Start fill monitor (scale emits [FILL_END] when target reached)
            _send_scale(self._scale_ser, f"G4 I1 W{target_g:.1f}")
            time.sleep(0.2)
            self._scale_ser.reset_input_buffer()

            # Start pump on hat
            _send(self._hat_ser, f"G2 I{pump_idx} D{_PUMP_MAX_MS}")
            log.info("[MACHINE] Pump %d ON", pump_idx)

            try:
                deadline = time.monotonic() + 65.0
                while time.monotonic() < deadline:
                    raw = self._scale_ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("ascii", errors="replace").strip()
                    if not line:
                        continue
                    log.debug("[SCALE] %s", line)

                    if not line.startswith("[FILL_END]"):
                        continue

                    reason    = "unknown"
                    dispensed = 0.0
                    for tok in line.split():
                        if tok.startswith("reason="):
                            reason = tok[len("reason="):]
                        elif tok.startswith("dispensed="):
                            try:
                                dispensed = float(tok[len("dispensed="):].rstrip("g"))
                            except ValueError:
                                pass

                    log.info("[MACHINE] Fill end – reason=%s  dispensed=%.1fg",
                             reason, dispensed)

                    if reason in ("empty_or_blocked", "pump_failure",
                                  "timeout", "hx711_error"):
                        raise MixerStall(
                            f"Pump stopped: {reason} "
                            f"(dispensed {dispensed:.1f}g of {target_g:.1f}g)")
                    return

                log.error("[MACHINE] pour_mixer: timed out waiting for [FILL_END]")
                raise MixerStall("Pump timeout – no [FILL_END] from scale ESP32")

            finally:
                _send(self._hat_ser, f"G2 I{pump_idx} D0")
                log.info("[MACHINE] Pump %d OFF", pump_idx)

    def stop_pump(self) -> None:
        with self._hw_lock:
            log.warning("[MACHINE] EMERGENCY – all pumps stopped")
            for idx in range(4):
                try:
                    _send(self._hat_ser, f"G2 I{idx} D0")
                except Exception as exc:
                    log.error("[MACHINE] stop_pump pump %d error: %s", idx, exc)
            try:
                _send(self._hat_ser, "M0.1")
            except Exception as exc:
                log.error("[MACHINE] stop_pump M0.1 error: %s", exc)

    # ── Scale (maintenance access only) ──────────────────────────────────────

    def tare_scale(self) -> None:
        with self._hw_lock:
            log.info("[MACHINE] Tare scale")
            _send_scale(self._scale_ser, "G3.1")
            time.sleep(1.0)

    def read_weight(self) -> float:
        with self._hw_lock:
            _send_scale(self._scale_ser, "G3")
            line = _scale_readline(self._scale_ser, timeout=3.0)
            if line and line.startswith("Weight:"):
                try:
                    g = float(line.split(":")[1].replace("g", "").strip())
                    log.info("[MACHINE] Scale read: %.2fg", g)
                    return g
                except ValueError:
                    pass
            log.warning("[MACHINE] Scale read failed: %s", line)
            return 0.0

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def close(self) -> None:
        for ser in (self._hat_ser, self._scale_ser):
            try:
                ser.close()
            except Exception:
                pass
