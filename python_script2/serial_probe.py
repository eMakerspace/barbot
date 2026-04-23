"""
Serial port auto-detection via M115 firmware identity query.

Each firmware responds to "M115\n" with a line containing:
    FIRMWARE_NAME:<name> ...

The probe opens every port under /dev/serial/by-id/, sends M115, waits up to
PROBE_TIMEOUT_S seconds for a matching response line, then closes the port.
Detected port paths are written back into hardware_config.json so subsequent
runs skip re-detection (unless the device is reconnected on a different path).
"""

import glob
import logging
import time

import serial

from config import HARDWARE_CONFIG_PATH, load_json, save_json

log = logging.getLogger("serial_probe")

PROBE_BAUD        = 115200
PROBE_TIMEOUT_S   = 3.0   # per-port wait for M115 response
INTER_CHAR_S      = 0.05  # pause between write and first read


def _probe_port(port: str, baud: int = PROBE_BAUD) -> str | None:
    """
    Open *port*, send M115, return the FIRMWARE_NAME value or None on failure.
    The port is always closed before returning.
    """
    try:
        with serial.Serial(port, baud, timeout=INTER_CHAR_S) as ser:
            ser.reset_input_buffer()
            ser.write(b"M115\n")
            ser.flush()

            deadline = time.monotonic() + PROBE_TIMEOUT_S
            buf = b""
            while time.monotonic() < deadline:
                chunk = ser.read(256)
                if chunk:
                    buf += chunk
                    # Scan complete lines for FIRMWARE_NAME
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("ascii", errors="replace").strip()
                        name = _parse_firmware_name(text)
                        if name:
                            return name
                else:
                    time.sleep(0.05)
    except serial.SerialException as exc:
        log.debug("[probe] %s: %s", port, exc)
    return None


def _parse_firmware_name(line: str) -> str | None:
    """Extract the FIRMWARE_NAME value from an M115 response line."""
    for token in line.split():
        if token.startswith("FIRMWARE_NAME:"):
            return token[len("FIRMWARE_NAME:"):]
    return None


def probe_and_update() -> dict[str, str]:
    """
    Scan all /dev/serial/by-id/ ports, identify each firmware via M115,
    and write the resolved port paths back into hardware_config.json.

    Returns a dict mapping firmware_name → resolved port path for all
    found devices.
    """
    cfg = load_json(HARDWARE_CONFIG_PATH)

    # Build a lookup: firmware_name → config key (e.g. "barbot-hat" → "serial")
    wanted: dict[str, str] = {}   # firmware_name → config_key
    for key in ("serial", "pump_serial", "neopixel_serial"):
        name = cfg.get(key, {}).get("firmware_name")
        if name:
            wanted[name] = key

    if not wanted:
        log.warning("[probe] No firmware_name entries found in hardware_config.json")
        return {}

    ports = sorted(glob.glob("/dev/serial/by-id/*"))
    if not ports:
        log.warning("[probe] No serial devices found under /dev/serial/by-id/")
        return {}

    log.info("[probe] Scanning %d port(s) for: %s", len(ports), list(wanted.keys()))

    found: dict[str, str] = {}   # firmware_name → port path
    for port in ports:
        if not wanted:
            break   # all targets matched
        log.debug("[probe] Probing %s …", port)
        name = _probe_port(port, PROBE_BAUD)
        if name and name in wanted:
            log.info("[probe] ✓ %s → %s", name, port)
            found[name] = port
            wanted.pop(name)
        elif name:
            log.debug("[probe] %s returned unknown firmware '%s' – ignored", port, name)

    if wanted:
        log.warning("[probe] Could not find: %s", list(wanted.keys()))

    if found:
        _write_ports(cfg, found)

    return found


def _write_ports(cfg: dict, found: dict[str, str]) -> None:
    """Update the port fields in cfg and save hardware_config.json."""
    # Invert: firmware_name → config_key
    name_to_key: dict[str, str] = {}
    for key in ("serial", "pump_serial", "neopixel_serial"):
        name = cfg.get(key, {}).get("firmware_name")
        if name:
            name_to_key[name] = key

    changed = False
    for name, port in found.items():
        key = name_to_key.get(name)
        if key and cfg[key].get("port") != port:
            log.info("[probe] Updating %s.port: %s → %s",
                     key, cfg[key].get("port"), port)
            cfg[key]["port"] = port
            changed = True

    if changed:
        save_json(cfg, HARDWARE_CONFIG_PATH)
        log.info("[probe] hardware_config.json updated")
