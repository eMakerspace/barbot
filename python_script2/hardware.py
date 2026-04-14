"""Hardware interface – real GPIO + ESP32 G-code serial implementation.

ESP32 G-code command reference (firmware: barbotv2):
  G28              – Home stepper (required before any movement). Responds with
                     "Homing successful, end pos = N" or "Homing failed: ..."
  G0 X{pos}        – Move stepper to absolute step position (0…end_pos).
                     No explicit done response; use G3 as a sync barrier.
  G0.1 X{fact}     – Move to fractional position (0.0…1.0 of full range).
  G1 Z{angle}      – Move servo to angle 0–180°. Fast (within one 20 ms PWM cycle).
  G2 I{n} D{ms}    – Run pump n (0–3) for ms milliseconds, non-blocking.
  G2.1 I{n} D{ms}  – Run pump n (0–3) for ms milliseconds, blocking.
  G3               – Read scale weight. Responds "Weight: X.XXg (raw: …)".
  G3.1             – Tare scale. Responds "Scale tared (offset: …)".
  G3.2 W{g}        – Calibrate scale with known weight. Responds "Calibrated: …".
  G4 I{n} W{g}     – Autonomous fill: run pump n until weight drops by g grams.
                     Responds "[FILL_END] reason=… dispensed=…g duration=…ms".
  T0 D{ms}         – Wait on ESP32 side for ms milliseconds (blocks command queue).
  M0               – Graceful stop (stepper decelerates).
  M0.1             – Immediate emergency stop (all motors/pumps off).
  M1               – Resume after stop.

Synchronisation strategy:
  route_cmd on the ESP32 serialises all commands through BiSignal, which
  blocks until the receiving task fully completes the operation.  This means
  the 128-deep CMD_CHANNEL acts as an ordered pipeline: commands execute
  strictly in the order they are sent.

  For commands with known responses (G28, G3, G3.1, G4) we wait for the
  expected string.  For pure motion (G0) there is no response, so we append a
  G3 scale-read to the queue and treat its response as a completion barrier.
"""

import queue
import re
import threading
import time

from config import BarbotConfig, HardwareConfig, SPIRIT_SLOTS, MIXER_SLOTS

# Module-level bounds used by lcd_menu before a HardwareInterface is instantiated.
X_MOVE_MIN = 0
X_MOVE_MAX = 6000

try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except ImportError:
    _GPIO_AVAILABLE = False
    print("[HW] RPi.GPIO not available – running in simulation mode")

try:
    import serial as _pyserial
    _SERIAL_AVAILABLE = True
except ImportError:
    _SERIAL_AVAILABLE = False
    print("[HW] pyserial not available – serial disabled")


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
        self._ser = _pyserial.Serial(port, baud, timeout=0.1)
        self._send_lock = threading.Lock()
        self._lines: queue.Queue[str] = queue.Queue()
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        print(f"[HW] Serial connected: {port} @ {baud}")

    def _reader(self):
        buf = b""
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
                        self._lines.put(line)
            except Exception:
                time.sleep(0.01)

    def send(self, cmd: str):
        """Send a single G-code command (appends \\n)."""
        with self._send_lock:
            self._ser.write((cmd + "\n").encode("utf-8"))

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

    def close(self):
        self._running = False
        try:
            self._ser.close()
        except Exception:
            pass


# ── Hardware interface ────────────────────────────────────────────────────────

class HardwareInterface:
    """Physical machine control via GPIO + ESP32 G-code serial.

    Falls back to simulation (timed sleeps, log prints) when the serial port
    is unavailable or not configured.
    """

    def __init__(self, config: BarbotConfig, hw_config: HardwareConfig):
        self.config = config
        self.hw = hw_config
        self.x_position: int = 0
        self._gpio_ready = False
        self._esp: EspSerial | None = None

        if _GPIO_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            self._gpio_ready = True
            print("[HW] GPIO ready.")

        if _SERIAL_AVAILABLE and hw_config.serial_port:
            try:
                self._esp = EspSerial(hw_config.serial_port, hw_config.serial_baud)
            except Exception as e:
                print(f"[HW] Serial unavailable ({e}) – simulation mode")
        else:
            if not hw_config.serial_port:
                print("[HW] No serial port configured – simulation mode")

    def cleanup(self):
        if _GPIO_AVAILABLE and self._gpio_ready:
            GPIO.cleanup()
        if self._esp:
            self._esp.close()

    # ── Cup sensor ───────────────────────────────────────────────

    def wait_for_cup(self):
        """Block until a cup is placed (simulated – no sensor connected)."""
        print("[HW] Waiting for cup...")
        time.sleep(1)
        print("[HW] Cup detected.")

    def wait_for_cup_removal(self):
        """Block until the cup is removed (simulated – no sensor connected)."""
        print("[HW] Waiting for cup to be taken...")
        time.sleep(1)
        print("[HW] Cup removed.")

    # ── Motion ───────────────────────────────────────────────────

    def homing(self):
        """Home the stepper motor (G28).  Must be called before any movement."""
        print("[HW] Homing...")
        if self._esp:
            self._esp.send("G28")
            line = self._esp.wait_for(
                ["Homing successful", "Homing failed"],
                timeout=120,
            )
            if "failed" in line.lower():
                raise HardwareError(f"Homing failed: {line}")
            # Parse end_pos from "Homing successful, end pos = N"
            try:
                self.hw.x_max = int(line.split("=")[-1].strip())
                print(f"[HW] Homing complete. Rail length = {self.hw.x_max} steps")
            except ValueError:
                print("[HW] Homing complete.")
            self.x_position = 0
        else:
            time.sleep(0.5)
            self.x_position = 0
            print("[HW] Homing complete (simulated).")

    def _queue_move(self, position: int):
        """Queue a G0 move without waiting for completion."""
        position = max(0, min(position, self.hw.x_max))
        if self._esp:
            self._esp.send(f"G0 X{position}")
        self.x_position = position

    def _sync(self, timeout: float = 30.0):
        """Send G3 (scale read) as a synchronisation barrier.

        Because route_cmd serialises all commands through BiSignal, this G3
        will only execute after all previously queued commands have completed.
        The "Weight:" (or scale-error) response confirms they are done.
        """
        if not self._esp:
            return
        self._esp.send("G3")
        try:
            self._esp.wait_for(
                ["Weight:", "raw:", "HX711 not", "cannot read", "not calibrated"],
                timeout=timeout,
            )
        except TimeoutError:
            print("[HW] WARNING: sync barrier timed out – assuming move complete")

    def move_x(self, position: int):
        """Move the X axis to *position* steps and wait for completion."""
        position = max(0, min(position, self.hw.x_max))
        distance = abs(position - self.x_position)
        print(f"[HW] Move X: {self.x_position} → {position} ({distance} steps)")
        if self._esp:
            self._queue_move(position)
            self._sync()
        else:
            travel_s = distance / self.hw.x_max
            time.sleep(travel_s * 0.5 + 0.05)
            self.x_position = position

    def move_to_idle(self):
        self.move_x(self.hw.x_idle)

    def _move_to_slot(self, slot: str):
        """Queue a move to *slot* without waiting (caller must sync later)."""
        pos = self.hw.position_for_slot(slot)
        if pos is None:
            raise HardwareError(f"No position configured for slot {slot!r}")
        self._queue_move(pos)

    # ── Spirit dispense (servo optic) ────────────────────────────

    def dispense_spirit(self, slot: str, pours: int, viscosity: float = 1.0):
        """Move to spirit slot and trigger the optic *pours* times.

        The servo actuates the optic: it tilts to `servo_pour_angle`, waits
        pour_duration_ms (scaled by viscosity), then returns to 180°
        (park position).  The entire sequence is pipelined to the ESP32 and
        we sync at the end so this call blocks until the last pour is done.
        """
        if pours <= 0:
            return
        self._move_to_slot(slot)
        angle = self.hw.servo_pour_angle
        pour_ms = int(self.hw.pour_duration_ms * viscosity)
        settle_ms = self.hw.settle_duration_ms
        print(
            f"[HW] Spirit slot {slot}: {pours} pour(s), "
            f"viscosity={viscosity:.2f}, {pour_ms}ms/pour"
        )
        if self._esp:
            for i in range(pours):
                print(f"[HW]   pour {i + 1}/{pours}")
                self._esp.send(f"G1 Z{angle}")
                self._esp.send(f"T0 D{pour_ms}")
                self._esp.send("G1 Z180")
                self._esp.send(f"T0 D{settle_ms}")
            # Sync: wait until all queued commands (move + pours) complete
            total_pour_s = pours * (pour_ms + settle_ms) / 1000
            self._sync(timeout=total_pour_s + 30)
        else:
            for i in range(pours):
                print(f"[HW]   pour {i + 1}/{pours}")
                time.sleep((pour_ms + settle_ms) / 1000)

    # ── Mixer dispense (peristaltic pump + load cell) ────────────

    def dispense_mixer(self, slot: str, ml: float):
        """Move to mixer slot and dispense *ml* millilitres via pump + scale.

        Mixer slots map to pumps: Slot_A→0, Slot_B→1, Slot_C→2, Slot_D→3.
        Uses G4 (autonomous fill) which runs the pump and monitors the load
        cell, stopping at 92% of target to compensate for overshoot.
        """
        if ml <= 0:
            return
        try:
            pump_idx = MIXER_SLOTS.index(slot)
        except ValueError:
            raise HardwareError(f"Slot {slot!r} is not a mixer slot")

        print(f"[HW] Mixer slot {slot} (pump {pump_idx}): {ml:.1f} ml")
        self._move_to_slot(slot)

        if self._esp:
            # Tare executes after the move completes (route_cmd serialises)
            self._esp.send("G3.1")
            self._esp.wait_for(
                "Scale tared",
                error_patterns=["HX711 not"],
                timeout=60,   # covers move time + tare
            )
            self._esp.send(f"G4 I{pump_idx} W{ml:.1f}")
            line = self._esp.wait_for("[FILL_END]", timeout=120)
            # Log dispensed amount parsed from "[FILL_END] reason=… dispensed=Xg …"
            try:
                for part in line.split():
                    if part.startswith("dispensed="):
                        print(f"[HW]   dispensed: {part.split('=')[1]}")
            except Exception:
                pass
        else:
            time.sleep(5)

    # ── Scale ────────────────────────────────────────────────────

    def tare_scale(self) -> str:
        """Tare (zero) the scale."""
        if self._esp:
            self._esp.send("G3.1")
            self._esp.wait_for(
                "Scale tared",
                error_patterns=["HX711 not"],
                timeout=10,
            )
            return "Scale tared."
        return "Scale tare (simulated)."

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

    def read_weight(self) -> float:
        """Read current weight from the scale.  Returns grams as float."""
        if self._esp:
            self._esp.send("G3")
            line = self._esp.wait_for(
                ["Weight:", "raw:", "HX711 not", "cannot read"],
                timeout=10,
            )
            g = self._parse_grams(line)
            return g if g is not None else 0.0
        return 0.0

    def read_weight_str(self) -> str:
        """Return a human-readable weight string for display on the LCD."""
        if self._esp:
            self._esp.send("G3")
            try:
                line = self._esp.wait_for(
                    ["Weight:", "raw:", "HX711 not", "cannot read"],
                    timeout=10,
                )
                g = self._parse_grams(line)
                if g is not None:
                    return f"Weight: {g:.1f}g"
                return f"Scale err: {line[:12]}"
            except TimeoutError:
                return "Scale timeout"
        return "Weight: 0.0g (sim)"

    # ── Emergency stop / resume ──────────────────────────────────

    def emergency_stop(self):
        """Send M0.1 (immediate stop): all motors and pumps halt instantly."""
        print("[HW] EMERGENCY STOP")
        if self._esp:
            self._esp.send("M0.1")

    def resume(self):
        """Send M1 (continue): clear emergency stop state on ESP32."""
        print("[HW] Resume")
        if self._esp:
            self._esp.send("M1")

    # ── High-level drink sequence ────────────────────────────────

    def make_drink(self, spec):
        """Full sequence for one DrinkSpec: cup → spirits → mixers → idle → done."""
        self.wait_for_cup()

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

        self.move_to_idle()
        self.wait_for_cup_removal()

    # ── Legacy compatibility stubs ───────────────────────────────

    def dispense(self, slot: str, ml: float):
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
        self.tare_scale()
        self.dispense_mixer(slot, float(grams))
        return f"{slot} cleaned {grams}g"

    def clean_spirit(self, slot: str, count: int) -> str:
        self.dispense_spirit(slot, count)
        return f"{slot} cleaned ×{count}"

    def check_slot_sensor(self, slot: str) -> bool:
        return slot not in self.config.empty_slots
