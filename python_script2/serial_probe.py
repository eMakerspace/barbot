"""
Serial port auto-detection via M115 firmware identity query.

Scans all /dev/serial/by-id/ ports, sends M115 at 115200 baud, and matches
responses containing FIRMWARE_NAME:<name> against the three expected devices:

  barbot-hat      → hardware_config.json "serial"
  barbot-scale    → hardware_config.json "pump_serial"
  barbot-display  → hardware_config.json "neopixel_serial"

Resolved port paths are written back into hardware_config.json.
Raises RuntimeError if any required device is not found.
"""

import glob
import logging
import time

import serial

from config import HARDWARE_CONFIG_PATH, load_json, save_json

log = logging.getLogger("probe")

PROBE_BAUD      = 115200
PROBE_TIMEOUT_S = 3.0
INTER_CHAR_S    = 0.05

REQUIRED_DEVICES = {"barbot-hat", "barbot-scale", "barbot-display"}

_FIRMWARE_TO_KEY = {
    "barbot-hat":     "serial",
    "barbot-scale":   "pump_serial",
    "barbot-display": "neopixel_serial",
}


def _probe_port(port: str) -> str | None:
    """Send M115; return FIRMWARE_NAME value or None."""
    ser = None
    try:
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = PROBE_BAUD
        ser.timeout = INTER_CHAR_S
        ser.dtr = False
        ser.rts = False
        ser.open()

        ser.reset_input_buffer()
        ser.write(b"M115\n")
        ser.flush()

        deadline = time.monotonic() + PROBE_TIMEOUT_S
        buf = b""
        while time.monotonic() < deadline:
            chunk = ser.read(256)
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("ascii", errors="replace").strip()
                    for token in text.split():
                        if token.startswith("FIRMWARE_NAME:"):
                            return token[len("FIRMWARE_NAME:"):]
            else:
                time.sleep(0.05)
    except (serial.SerialException, TimeoutError, OSError) as exc:
        log.debug("[probe] %s: %s", port, exc)
    finally:
        if ser and ser.is_open:
            try:
                ser.close()
            except Exception:
                pass
    return None


def probe_and_update() -> dict[str, str]:
    """
    Scan all /dev/serial/by-id/ ports, identify each firmware via M115,
    write resolved ports into hardware_config.json, and return the found map.

    Raises RuntimeError if any required device is missing.
    """
    ports = sorted(glob.glob("/dev/serial/by-id/*"))
    if not ports:
        raise RuntimeError("No serial devices found under /dev/serial/by-id/")

    log.info("[probe] Scanning %d port(s) for: %s", len(ports), sorted(REQUIRED_DEVICES))

    wanted = set(REQUIRED_DEVICES)
    found: dict[str, str] = {}

    for port in ports:
        if not wanted:
            break
        log.debug("[probe] Probing %s …", port)
        name = _probe_port(port)
        if name and name in wanted:
            log.info("[probe] ✓ %s → %s", name, port)
            found[name] = port
            wanted.discard(name)
        elif name:
            log.debug("[probe] %s: unknown firmware '%s' – ignored", port, name)
        else:
            log.debug("[probe] %s: no M115 response", port)

    if wanted:
        raise RuntimeError(f"Required devices not found: {sorted(wanted)}")

    cfg = load_json(HARDWARE_CONFIG_PATH)
    _write_ports(cfg, found)
    return found


def _write_ports(cfg: dict, found: dict[str, str]) -> None:
    changed = False
    for name, port in found.items():
        key = _FIRMWARE_TO_KEY[name]
        section = cfg.setdefault(key, {})
        if section.get("port") != port:
            log.info("[probe] Updating %s.port: %s → %s", key, section.get("port"), port)
            section["port"] = port
            changed = True
    if changed:
        save_json(cfg, HARDWARE_CONFIG_PATH)
        log.info("[probe] hardware_config.json updated")
