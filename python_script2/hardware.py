"""Hardware interface – real GPIO + two ESP32 G-code serial connections.

HAT ESP32 (barbotv2) — stepper, servo, cup sensor:
  G28              – Home stepper. Responds "Homing successful, end pos = N".
  G0 X{pos}        – Move stepper to absolute step position. Responds "Move done".
  G0.1 X{fact}     – Move to fractional position (0.0…1.0 of full range).
  G1 Z{angle}      – Move servo to angle 0–180°.
  G5 P{p4} Q{p5}   – Set forbidden servo zone (slot 4 / slot 5 positions).
  T0 D{ms}         – Wait ms milliseconds (blocks HAT command queue).
  M0               – Graceful stop (stepper decelerates).
  M0.1             – Immediate emergency stop (stepper/servo off).
  M1               – Resume after stop.
  M50              – Query cup presence state. Responds "[cup] PRESENT" or "[cup] ABSENT".

Pump ESP32 (esp32_pump) — pumps, scale, fill:
  G2 I{n} D{ms}    – Run pump n (0–3) for ms milliseconds, non-blocking.
  G2.1 I{n} D{ms}  – Run pump n (0–3) for ms milliseconds, blocking.
  G3               – Read scale weight. Responds "Weight: X.XXg (raw: …)".
  G3.1             – Tare scale. Responds "Scale tared (offset: …)".
  G3.2 W{g}        – Calibrate scale with known weight. Responds "Calibrated: …".
  G3.3 F{cpg}      – Restore calibration factor (sent at boot).
  G4 I{n} W{g}     – Autonomous fill: run pump n until weight drops by g grams.
                     Responds "[FILL_END] reason=… dispensed=…g duration=…ms".
  M0 / M0.1 / M1  – Stop / resume pumps.

Synchronisation strategy:
  HAT route_cmd serialises all commands through BiSignal, so commands execute
  strictly in order.  "Move done" is the completion signal for G0 moves.
  For mixer dispense, _sync() waits for "Move done" before sending tare/fill
  to the pump ESP, ensuring the carriage is at the slot before dispensing.
"""

import json
import os
import queue
import re
import threading
import time
from pathlib import Path

from config import BarbotConfig, HardwareConfig, SPIRIT_SLOTS, MIXER_SLOTS
from logger import log_debug, log_info, log_warn, log_error

# Path to the hardware config JSON — calibration factor is persisted here.
_HW_CONFIG_PATH = Path(__file__).parent / "config" / "hardware_config.json"

# Regex that matches the calibration confirmation line from the ESP32.
_CALIBRATION_RE = re.compile(r"Calibrated:\s*([\d.]+)\s*counts/gram")

# Regex that matches error state messages from ESP32: [ERROR_STATE] code=N name=... effect=...
# name can contain spaces (e.g. "HX711 Not Detected"), so use non-greedy match up to effect=
_ERROR_STATE_RE = re.compile(r"\[ERROR_STATE\]\s+code=(\d+)\s+name=(.*?)\s+effect=(\S+)\s+severity=(\d+)")

# Regex that matches cup presence messages from ESP32: [cup] PRESENT / [cup] ABSENT
_CUP_RE = re.compile(r"\[cup\]\s+(PRESENT|ABSENT)")
# Regex that extracts fill completion reason from ESP32: [FILL_END] reason=...
_FILL_END_REASON_RE = re.compile(r"\[FILL_END\]\s+reason=([a-z_]+)")


# Regex to strip ANSI escape codes from ESP32 log lines
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mK]')


try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except ImportError:
    _GPIO_AVAILABLE = False
    raise RuntimeError("RPi.GPIO library not available - GPIO functionality disabled")

try:
    import serial as _pyserial
    _SERIAL_AVAILABLE = True
except ImportError:
    _SERIAL_AVAILABLE = False
    raise RuntimeError("pyserial library not available - serial functionality disabled")


# ── Neopixel/7-Segment serial driver (fire-and-forget) ──────────────────────

class NeopixelSerial:
    """Fire-and-forget serial connection to the neopixel/7-segment ESP32.

    Unlike EspSerial, this does not read responses or support wait_for().
    Commands are sent asynchronously with minimal overhead.
    """

    def __init__(self, port: str, baud: int = 115200):
        try:
            log_info("NEO", f"Connecting to neopixel controller on {port} @ {baud} baud...")
            self._ser = _pyserial.Serial(port, baud, timeout=0.1)
            log_info("NEO", "Connected to neopixel controller")
        except Exception as e:
            log_error("NEO", f"Failed to open neopixel serial port: {e}")
            raise

        self._lock = threading.Lock()

    def send(self, cmd: str):
        """Send a command to the neopixel ESP32 (non-blocking)."""
        try:
            with self._lock:
                self._ser.write((cmd.strip() + "\n").encode())
        except Exception as e:
            log_warn("NEO", f"Send failed: {e}")


class HardwareError(Exception):
    pass


# ── ESP32 serial driver ───────────────────────────────────────────────────────

class EspSerial:
    """Thread-safe serial connection to the ESP32 barbot firmware.

    A background daemon thread reads lines from the serial port and puts them
    into an unbounded queue.  Callers send G-code strings with send() and block
    on specific response patterns with wait_for().
    """

    def __init__(self, port: str, baud: int = 115200):
        try:
            log_info("HWSER", f"Connecting to {port} @ {baud} baud...")
            self._ser = _pyserial.Serial(port, baud, timeout=0.1)
            log_info("HWSER", f"Serial port opened successfully")
        except Exception as e:
            log_error("HWSER", f"Failed to open serial port: {e}")
            raise

        self._send_lock = threading.Lock()
        self._lines: queue.Queue[str] = queue.Queue()
        # Optional callback called with the counts-per-gram factor whenever
        # the ESP32 confirms a successful G3.2 calibration.
        self._on_calibrated: "callable | None" = None
        # Optional callback called when an error state is detected on ESP32
        self._on_error_state: "callable | None" = None
        # Optional callback called when cup state changes: cb(present: bool)
        self._on_cup_state: "callable | None" = None
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        log_info("HWSER", f"Serial reader thread started")

    def _reader(self):
        buf = b""
        log_info("HWSER", "Serial reader thread running")
        while self._running:
            try:
                chunk = self._ser.read(128)
                if not chunk:
                    continue
                buf += chunk
                # Normalise all line endings to \n, then split
                lines = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
                buf = lines[-1]          # keep partial last fragment
                for raw in lines[:-1]:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        print(f"[ESP32] {line}")
                        log_debug("HWSER", f"ESP32 → {line}")
                        self._lines.put(line)

                        # Check for calibration messages
                        m = _CALIBRATION_RE.search(line)
                        if m and self._on_calibrated:
                            try:
                                factor = float(m.group(1))
                                log_info("HWSER", f"Calibration detected: {factor} counts/gram")
                                self._on_calibrated(factor)
                            except Exception as e:
                                log_warn("HWSER", f"Calibration callback failed: {e}")

                        # Check for error state messages
                        m = _ERROR_STATE_RE.search(line)
                        if m and self._on_error_state:
                            try:
                                code = int(m.group(1))
                                name = m.group(2)
                                effect = m.group(3)
                                severity = int(m.group(4))
                                log_info("HWSER", f"Error state detected: code={code} name={name} effect={effect} severity={severity}")
                                self._on_error_state(code, name, effect, severity)
                            except Exception as e:
                                log_warn("HWSER", f"Error state callback failed: {e}")

                        # Check for cup state messages
                        m = _CUP_RE.search(line)
                        if m and self._on_cup_state:
                            try:
                                present = m.group(1) == "PRESENT"
                                self._on_cup_state(present)
                            except Exception as e:
                                log_warn("HWSER", f"Cup state callback failed: {e}")
            except Exception as e:
                log_warn("HWSER", f"Serial reader exception: {e}")
                time.sleep(0.01)

    def send(self, cmd: str):
        """Send a single G-code command (appends \\n)."""
        try:
            with self._send_lock:
                log_debug("HWSER", f"ESP32 ← {cmd}")
                self._ser.write((cmd + "\n").encode("utf-8"))
        except Exception as e:
            log_error("HWSER", f"Failed to send command '{cmd}': {e}")

    def wait_for(
        self,
        patterns: "str | list[str]",
        error_patterns: "list[str] | None" = None,
        timeout: float = 60.0,
    ) -> str:
        """Block until a line matching any of *patterns* arrives.

        If a line matches any *error_patterns* a HardwareError is raised
        immediately.  Raises TimeoutError when the deadline is exceeded.
        """
        if isinstance(patterns, str):
            patterns = [patterns]
        error_patterns = error_patterns or []
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                line = self._lines.get(timeout=min(remaining, 0.2))
                for ep in error_patterns:
                    if ep in line:
                        raise HardwareError(f"ESP32: {line}")
                if any(p in line for p in patterns):
                    return line
            except queue.Empty:
                pass

        raise TimeoutError(
            f"Timeout ({timeout}s) waiting for {patterns!r} from ESP32"
        )


# ── Hardware interface ────────────────────────────────────────────────────────

class HardwareInterface:
    """Physical machine control via GPIO + ESP32 G-code serial.
    """

    def __init__(self, config: BarbotConfig, hw_config: HardwareConfig):
        self.config = config
        self.hw = hw_config
        self.x_position: int = 0
        self._gpio_ready = False
        self._esp: EspSerial | None = None        # barbotv2: stepper / servo / cup
        self._pump_esp: EspSerial | None = None   # esp32_pump: pumps / scale / fill
        self._neo: NeopixelSerial | None = None
        self.serial_error: str | None = None   # set if serial init failed
        self.ui = None  # injected reference to LCDMenu
        self._last_order_id: int = 0  # track order ID for display

        if _GPIO_AVAILABLE:
            try:
                GPIO.setmode(GPIO.BCM)
                self._gpio_ready = True
                log_info("HWINT", "GPIO initialized successfully")
            except Exception as e:
                log_warn("HWINT", f"GPIO initialization failed: {e}")
        else:
            log_warn("HWINT", "RPi.GPIO not available")

        # Cup sensor state is reported by the ESP32 via serial ("[cup] PRESENT" / "[cup] ABSENT").
        # The Pi does NOT read GPIO2 directly — GPIO2 on the Pi is I2C SDA (used by the LCD).
        self._cup_lock = threading.Lock()  # Protects _cup_present and event loop synchronization
        self._cup_present: bool = False
        self._cup_state_changed = threading.Event()

        if _SERIAL_AVAILABLE and hw_config.serial_port:
            try:
                log_info("HWINT", f"Initializing HAT serial (barbotv2) on {hw_config.serial_port}")
                self._esp = EspSerial(hw_config.serial_port, hw_config.serial_baud)
                self._esp._on_error_state = self._on_error_state
                self._esp._on_cup_state = self._on_cup_state_changed
                self._restore_servo_zones()
                # Park servo at safe closed position on startup
                self._esp.send(f"G1 Z{hw_config.servo_close_angle}")
                log_info("HWINT", "HAT serial ready")
            except Exception as e:
                self.serial_error = str(e)
                raise RuntimeError(f"HAT serial initialization failed: {e}")
        else:
            if not hw_config.serial_port:
                self.serial_error = "No HAT serial port configured"
                raise RuntimeError("No HAT serial port configured")
            else:
                raise RuntimeError("pyserial library not available - serial functionality disabled")

        if _SERIAL_AVAILABLE and hw_config.pump_port:
            try:
                log_info("HWINT", f"Initializing pump serial (esp32_pump) on {hw_config.pump_port}")
                self._pump_esp = EspSerial(hw_config.pump_port, hw_config.pump_baud)
                self._pump_esp._on_calibrated = self._save_calibration_factor
                self._restore_calibration()
                log_info("HWINT", "Pump serial ready")
            except Exception as e:
                raise RuntimeError(f"Pump serial initialization failed: {e}")
        else:
            if not hw_config.pump_port:
                raise RuntimeError("No pump serial port configured")

        # Initialize neopixel/7-segment ESP32 if configured
        if _SERIAL_AVAILABLE and hw_config.neo_port:
            try:
                self._neo = NeopixelSerial(hw_config.neo_port, hw_config.neo_baud)
                self._neo.send("-i")   # start idle animations on startup
                log_info("HWINT", "Neopixel controller initialized")
            except Exception as e:
                raise RuntimeError(f"Neopixel serial initialization failed: {e}")
    # ── Scale calibration persistence ────────────────────────────

    def _save_calibration_factor(self, factor: float):
        """Write the counts-per-gram factor into hardware_config.json."""
        try:
            cfg = json.loads(_HW_CONFIG_PATH.read_text())
            cfg["scale_calibration_factor"] = round(factor, 6)
            _HW_CONFIG_PATH.write_text(json.dumps(cfg, indent=4))
            print(f"[HW] Calibration saved: {factor:.4f} counts/gram")
        except Exception as e:
            print(f"[HW] Failed to save calibration: {e}")

    def _restore_calibration(self):
        """Send G3.3 to the pump ESP32 to restore the previously saved calibration."""
        try:
            cfg = json.loads(_HW_CONFIG_PATH.read_text())
            factor = cfg.get("scale_calibration_factor")
            if factor and isinstance(factor, (int, float)) and factor > 0:
                self._pump_esp.send(f"G3.3 F{factor:.6f}")
                print(f"[HW] Calibration restored: {factor:.4f} counts/gram")
            else:
                print("[HW] No saved calibration — pump ESP32 will use firmware default")
        except Exception as e:
            print(f"[HW] Could not restore calibration: {e}")

    def _restore_servo_zones(self):
        """Send G5 to the ESP32 with slot 4 and slot 5 positions for forbidden zone calculation."""
        try:
            cfg = json.loads(_HW_CONFIG_PATH.read_text())
            slots = cfg.get("slot_positions", {})
            p4 = slots.get("Slot_4")
            p5 = slots.get("Slot_5")
            if p4 is not None and p5 is not None:
                self._esp.send(f"G5 P{p4} Q{p5}")
                print(f"[HW] Servo forbidden zone set: Slot_4={p4}, Slot_5={p5}")
            else:
                print("[HW] Slot_4 or Slot_5 position not configured — servo zone blocking disabled")
        except Exception as e:
            print(f"[HW] Could not restore servo zones: {e}")

    def _on_cup_state_changed(self, present: bool):
        """Called from serial reader thread when ESP32 reports cup state change."""
        with self._cup_lock:
            self._cup_present = present
        self._cup_state_changed.set()
        log_info("HWINT", f"Cup state: {'PRESENT' if present else 'ABSENT'}")

    def _on_error_state(self, code: int, name: str, effect: str, severity: int):
        """Handle error state from ESP32: update LED and LCD display."""
        log_info("HWINT", f"Error from ESP32: code={code} name={name} effect={effect} severity={severity}")

        # Send LED effect to neopixel controller if available
        if self._neo:
            try:
                self._neo.send(f"-e {effect}")  # Send LED effect command
                log_debug("HWINT", f"Sent LED effect to neopixel: {effect}")
            except Exception as e:
                log_warn("HWINT", f"Failed to send LED effect: {e}")

        # Update LCD display if available
        if self.ui:
            try:
                self.ui.show_error(name, severity)
            except Exception as e:
                log_warn("HWINT", f"Failed to update LCD: {e}")

    def cleanup(self):
        log_info("HWINT", "Starting hardware cleanup...")
        if _GPIO_AVAILABLE and self._gpio_ready:
            try:
                GPIO.cleanup()
                log_info("HWINT", "GPIO cleanup complete")
            except Exception as e:
                log_warn("HWINT", f"GPIO cleanup error: {e}")
        
        log_info("HWINT", "Hardware cleanup finished")

    # ── Cup sensor ───────────────────────────────────────────────

    def wait_for_cup(self):
        """Block until a cup is detected and confirmed via ESP32 light curtain sensor.

        The ESP32 monitors GPIO2 (light curtain) and reports state changes via serial
        as "[cup] PRESENT" / "[cup] ABSENT". The Pi must NOT read GPIO2 directly
        (it is I2C SDA on the Pi, used by the LCD).

        After the cup is detected, waits CUP_CONFIRM_DELAY seconds (with blink
        animation) before returning, so the user has time to position their cup.

        Raises HardwareError if no cup is placed within timeout_sec seconds.
        """
        log_info("HWINT", f"Waiting for cup...")

        if not self._esp:
            raise HardwareError("Serial port unavailable — cannot detect cup")

        # Query current cup state from ESP32 (M50 command)
        self._esp.send("M50")
        try:
            self._esp.wait_for("[cup]", timeout=5)
        except TimeoutError:
            log_warn("HWINT", "Cup state query timed out, proceeding with cached state")

        with self._cup_lock:
            cup_present = self._cup_present
            if not cup_present:
                # Reset event before showing UI (prevents lost wakeup)
                self._cup_state_changed.clear()

        if cup_present:
            # Cup already present, skip waiting
            log_info("HWINT", "Cup detected")
            self._cup_confirm_countdown()
            return

        # Cup was absent when we checked, now wait for it
        if self.ui:
            self.ui.show_add_cup()
        if self._neo:
            self._neo.send("-c")

        while True:
            self._cup_state_changed.wait()  # Safe: event was cleared under lock

            with self._cup_lock:
                self._cup_state_changed.clear()
                if self._cup_present:
                    log_info("HWINT", "Cup detected")
                    cup_present = True
                else:
                    cup_present = False

            if cup_present:
                break

        self._cup_confirm_countdown()

    # Countdown seconds before dispensing starts after a cup is placed.
    CUP_CONFIRM_SECS = 6

    def _cup_confirm_countdown(self):
        """6-second countdown with slow→fast green flash and 7-seg digit countdown.

        Once started, runs to completion regardless of sensor state.
        """
        total = self.CUP_CONFIRM_SECS
        log_info("HWINT", f"Cup confirmed — starting {total}s countdown...")
        count_start = time.monotonic()
        for step, remaining in enumerate(range(total, 0, -1)):
            if remaining > total // 2:
                if self._neo:
                    self._neo.send("-e GREEN_FLASH_SLOW")
            else:
                if self._neo:
                    self._neo.send("-e GREEN_FLASH_FAST")
            if self._neo:
                self._neo.send(str(remaining))
            tick_end = count_start + step + 1
            wait = tick_end - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        log_info("HWINT", "Cup countdown done, starting dispense.")
        if self._neo:
            self._neo.send(f"-br {self._last_order_id % 100}")
        if self.ui:
            self.ui.restore_mixing()

    def wait_for_cup_removal(self):
        """Wait for cup to be removed after drink is done.
        """
        log_info("HWINT", f"Drink done — waiting for cup removal...")


        if not self._esp:
            raise HardwareError("Serial port unavailable — cannot detect cup removal")


        num = self._last_order_id % 100

        # Phase 1: attention animations
        if self._neo:
            self._neo.send(f"-bl {num}")
            time.sleep(0.1)
            self._neo.send("-done")

        with self._cup_lock:
            # Reset event before entering wait loop
            self._cup_state_changed.clear()

        while True:
            self._cup_state_changed.wait(timeout=0.5)

            with self._cup_lock:
                self._cup_state_changed.clear()
                if not self._cup_present:
                    cup_removed = True
                else:
                    cup_removed = False

            if cup_removed:
                log_info("HWINT", "Cup removed!")
                if self.ui:
                    self.ui.clear_mixing()
                if self._neo:
                    self._neo.send("-i")
                time.sleep(1)
                return

    # ── Motion ───────────────────────────────────────────────────

    def homing(self):
        """Home the stepper motor (G28), then move to idle position.

        **SAFETY:** Ensures servo is at safe close_angle before and after homing.
        """
        log_info("HWINT", "Homing stepper motor...")
        if not self._esp:
            raise HardwareError("Serial port unavailable — cannot home stepper")

        try:
            # Park servo at safe angle BEFORE homing
            log_info("HWINT", f"Parking servo at safe angle ({self.hw.servo_close_angle}°)...")
            self._esp.send(f"G1 Z{self.hw.servo_close_angle}")
            self._esp.send(f"T0 D{self.hw.settle_duration_ms}")

            self._esp.send("G28")
            line = self._esp.wait_for(
                ["Homing successful", "Homing failed"],
                timeout=360,
            )
            if "failed" in line.lower():
                log_error("HWINT", f"Homing failed: {line}")
                raise HardwareError(f"Homing failed: {line}")
            # Parse end_pos from "Homing successful, end pos = N"
            # Strip ANSI codes first (firmware log lines contain colour escapes).
            try:
                clean = _ANSI_RE.sub('', line)
                self.hw.x_max = int(clean.split("=")[-1].strip())
                log_info("HWINT", f"Homing successful. Rail length = {self.hw.x_max} steps")
            except ValueError:
                log_info("HWINT", "Homing successful")
            # After homing, stepper is at the positive end switch (x_max position)
            self.x_position = self.hw.x_max

            # Move to idle position to complete homing sequence
            log_info("HWINT", f"Moving to idle position ({self.hw.x_idle} steps)...")
            self.move_to_idle()
            log_info("HWINT", "Homing complete, at idle position")
        except Exception as e:
            log_error("HWINT", f"Homing failed with exception: {e}")
            raise

    def _queue_move(self, position: int):
        """Queue a G0 move without waiting for completion."""
        position = max(0, min(position, self.hw.x_max))
        if self._esp:
            cmd = f"G0 X{position}"
            if self.hw.x_accel is not None:
                cmd += f" A{self.hw.x_accel}"
            if self.hw.x_max_speed is not None:
                cmd += f" S{self.hw.x_max_speed}"
            self._esp.send(cmd)
        self.x_position = position

    def _sync(self, timeout: float = 30.0):
        """Wait for the HAT command queue to drain using 'Move done' as a barrier.

        Sends a no-op G0 to the current position so route_cmd serialises it
        through BiSignal; the 'Move done' response confirms all prior commands
        have completed.  Falls back silently if HAT serial is unavailable.
        """
        if not self._esp:
            return
        self._esp.send(f"G0 X{self.x_position}")
        try:
            self._esp.wait_for(["Move done"], timeout=timeout)
        except TimeoutError:
            raise HardwareError(
                f"Carriage sync timed out after {timeout}s — stepper may be jammed. "
                f"Last known position: {self.x_position}"
            )

    def move_x(self, position: int):
        """Move the X axis to *position* steps and wait for completion."""
        if not self._esp:
            raise HardwareError("Serial port unavailable — cannot move stepper")

        position = max(0, min(position, self.hw.x_max))
        distance = abs(position - self.x_position)
        print(f"[HW] Move X: {self.x_position} → {position} ({distance} steps)")
        self._queue_move(position)
        try:
            self._esp.wait_for(["Move done"], timeout=30.0)
        except TimeoutError:
            raise HardwareError(
                f"Carriage move to X={position} timed out after 30s — stepper may be jammed. "
                f"Current position unknown, aborting to prevent collision."
            )

    def move_to_idle(self):
        """Move to idle position with servo at safe close_angle.

        **CRITICAL SAFETY:** Servo must be at close_angle BEFORE any stepper move
        to avoid collision with the moving carriage. This function ensures the
        servo is parked both before and after the movement.

        Returns with servo at close_angle and carriage at idle position.
        """
        # Moving animation
        if self._neo:
            self._neo.send("-mv")
        if self._esp:
            # Park servo at safe angle BEFORE moving carriage
            self._esp.send(f"G1 Z{self.hw.servo_close_angle}")
            self._esp.send(f"T0 D{self.hw.settle_duration_ms}")
        self.move_x(self.hw.x_idle)
        if self._esp:
            # Ensure servo remains parked AFTER carriage move
            self._esp.send(f"G1 Z{self.hw.servo_close_angle}")
            self._sync(timeout=5)

    @staticmethod
    def _parse_fill_end_reason(line: str) -> str:
        """Extract fill completion reason from a [FILL_END] line."""
        m = _FILL_END_REASON_RE.search(line)
        if not m:
            raise HardwareError(f"Invalid fill response from ESP32: {line}")
        return m.group(1)

    # ── Central pour sequence (servo optic) ──────────────────────

    def _pour_sequence(self, pours: int, pour_duration_ms: int, settle_duration_ms: int):
        """Execute a standardized pour sequence: open→pour→close, repeated.

        **CRITICAL SAFETY:**
        - Caller MUST ensure servo is at close_angle BEFORE calling this.
        - This function ONLY moves servo to pour_angle and back to close_angle.
        - After this function returns, servo is GUARANTEED at close_angle.
        - This is the ONLY function that moves servo to pour_angle.

        Args:
            pours: Number of times to repeat the pour cycle.
            pour_duration_ms: Time servo holds open (milliseconds).
            settle_duration_ms: Time to wait between pours.
        """
        if not self._esp:
            raise HardwareError("Serial port unavailable — cannot dispense spirits")

        pour_angle = self.hw.servo_pour_angle
        close_angle = self.hw.servo_close_angle

        for i in range(pours):
            print(f"[HW]   pour {i + 1}/{pours}")
            self._esp.send(f"G1 Z{pour_angle}")
            # Delay to allow servo to reach and settle at pour_angle (avoids jitter)
            self._esp.send(f"T0 D{self.hw.servo_settle_ms}")
            self._esp.send(f"T0 D{pour_duration_ms}")
            self._esp.send(f"G1 Z{close_angle}")
            # Delay to allow servo to settle at close_angle before next cycle
            self._esp.send(f"T0 D{self.hw.servo_settle_ms}")
            if i < pours - 1:
                self._esp.send(f"T0 D{settle_duration_ms}")

        # Sync: wait until all pours complete
        total_pour_s = pours * pour_duration_ms / 1000 + max(0, pours - 1) * settle_duration_ms / 1000
        self._sync(timeout=total_pour_s + 30)

    # ── Spirit dispense (servo optic) ────────────────────────────

    def dispense_spirit(self, slot: str, pours: int, viscosity: float = 1.0):
        """Move to spirit slot and trigger the optic *pours* times.

        Uses the centralized pour sequence which ensures servo safety.
        After pouring, servo is guaranteed to be at safe close_angle.

        **SAFETY:** Servo is parked at close_angle BEFORE moving carriage.
        """
        pos = self.hw.position_for_slot(slot)
        if pos is None:
            raise HardwareError(f"No position configured for slot {slot!r}")
        zone = self.hw.in_forbidden_servo_zone(pos)
        if zone:
            print(f"[HW] BLOCKED: servo open at slot {slot} (pos={pos}) forbidden — {zone}")
            return

        if self._neo:
            self._neo.send("-mv")

        self.move_x(pos)  # blocks until sled is at slot

        if self._neo:
            self._neo.send("-pour")

        pour_ms = int(self.hw.pour_duration_ms * viscosity)
        settle_ms = self.hw.settle_duration_ms
        print(
            f"[HW] Spirit slot {slot}: {pours} pour(s), "
            f"viscosity={viscosity:.2f}, {pour_ms}ms/pour"
        )

        self._pour_sequence(pours, pour_ms, settle_ms)
        # SAFETY: Servo is now at close_angle, which is safe

    # ── Mixer dispense (peristaltic pump + load cell) ────────────

    def dispense_mixer(self, slot: str, ml: float):
        """Move to mixer slot and dispense *ml* millilitres via pump + scale.

        Mixer slots map to pumps: Slot_A→0, Slot_B→1, Slot_C→2, Slot_D→3.
        Uses G4 (autonomous fill) which runs the pump and monitors the load
        cell, stopping at 92% of target to compensate for overshoot.

        **SAFETY:** Servo is parked at close_angle before and after dispensing.
        """
        if ml <= 0:
            return
        try:
            pump_idx = MIXER_SLOTS.index(slot)
        except ValueError:
            raise HardwareError(f"Slot {slot!r} is not a mixer slot")

        if not self._pump_esp:
            raise HardwareError("Pump serial port unavailable — cannot dispense mixers")

        print(f"[HW] Mixer slot {slot} (pump {pump_idx}): {ml:.1f} ml")

        pos = self.hw.position_for_slot(slot)
        if pos is None:
            raise HardwareError(f"No position configured for slot {slot!r}")

        if self._neo:
            self._neo.send("-mv")

        self.move_x(pos)  # blocks until sled is at slot

        if self._neo:
            self._neo.send("-mix")

        comp = self.hw.pump_tubing_compensation_g
        target_g = ml + comp
        self._pump_esp.send(f"G4 I{pump_idx} W{target_g:.1f}")
        line = self._pump_esp.wait_for("[FILL_END]", timeout=35)
        reason = self._parse_fill_end_reason(line)
        if reason not in {"target_reached", "zero_target"}:
            raise HardwareError(f"Fill failed ({reason}): {line}")
        try:
            for part in line.split():
                if part.startswith("dispensed="):
                    print(f"[HW]   dispensed: {part.split('=')[1]}")
        except Exception:
            pass

    # ── Scale ────────────────────────────────────────────────────

    def tare_scale(self) -> str:
        """Tare (zero) the scale."""
        if not self._pump_esp:
            raise HardwareError("Pump serial port unavailable — cannot tare scale")

        self._pump_esp.send("G3.1")
        self._pump_esp.wait_for(
            "Scale tared",
            error_patterns=["HX711 not"],
            timeout=10,
        )
        return "Scale tared."

    def calibrate_scale(self, known_grams: float) -> str:
        """Calibrate scale with a known weight already on the platform (no tare).

        Assumes tare_scale() was already called with the scale empty.
        Sends G3.2 W{grams} and waits for confirmation.
        """
        if not self._pump_esp:
            raise HardwareError("Pump serial port unavailable — cannot calibrate scale")

        self._pump_esp.send(f"G3.2 W{known_grams:.1f}")
        line = self._pump_esp.wait_for(
            ["Calibrated:", "Calibration failed"],
            error_patterns=["HX711 not"],
            timeout=10,
        )
        if "failed" in line.lower():
            return f"Calibration failed: {line}"
        return f"Calibrated OK: {line.split('Calibrated:')[-1].strip()}"

    _WEIGHT_RE = re.compile(r"(?:Weight:|weight:)\s*(-?[\d.]+)\s*g", re.IGNORECASE)

    def _parse_grams(self, line: str) -> "float | None":
        """Extract grams from a weight line regardless of log prefix."""
        # Handles: "Weight: 12.34g (raw: …)"
        #          "INFO Weight: 12.34g (raw: …)"
        #          "INFOWeight: 12.34g (raw: …)"  (firmware log artifact)
        m = self._WEIGHT_RE.search(line)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        # Fallback: look for a bare float followed by 'g' anywhere in the line
        m2 = re.search(r"(-?[\d.]+)\s*g\b", line)
        if m2:
            try:
                return float(m2.group(1))
            except ValueError:
                pass
        return None

    def read_weight_str(self) -> str:
        """Return a human-readable weight string for display on the LCD."""
        if not self._pump_esp:
            raise HardwareError("Pump serial port unavailable — cannot read weight")

        self._pump_esp.send("G3")
        try:
            line = self._pump_esp.wait_for(
                ["Weight:", "raw:", "HX711 not", "cannot read"],
                timeout=10,
            )
            g = self._parse_grams(line)
            if g is not None:
                return f"Weight: {g:.1f}g"
            return f"Scale err: {line[:12]}"
        except TimeoutError:
            return "Scale timeout"

    # ── High-level drink sequence ────────────────────────────────

    def make_drink(self, spec):
        """Full sequence for one DrinkSpec: cup → spirits → mixers → idle.

        Does NOT wait for cup removal—that's handled by the caller to allow
        error handling with animations before removal. Caller must ensure cup
        removal is handled (even on error).

        Args:
            spec: DrinkSpec with spirits and mixers to dispense
        """
        num = self._last_order_id % 100

        try:
            self.wait_for_cup()
            if self._neo:
                self._neo.send(f"-br {num}")

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
        finally:
            self.move_to_idle()

    # ── Legacy compatibility stubs ───────────────────────────────

    def display_order_id(self, short_id: int):
        """Display order ID on neopixel 7-segment display."""
        self._last_order_id = short_id
        if self._neo:
            self._neo.send(f"-br {short_id % 100}")  # breathe number while preparing

    def clean_mixer(self, slot: str, grams: int) -> str:
        self.tare_scale()
        self.dispense_mixer(slot, float(grams))
        return f"{slot} cleaned {grams}g"

    def clean_spirit(self, slot: str, count: int) -> str:
        self.dispense_spirit(slot, count)
        # After cleaning, move to idle (which parks servo at safe position)
        self.move_to_idle()
        return f"{slot} cleaned ×{count}"
