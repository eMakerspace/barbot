#!/usr/bin/env python3
"""Display the 152.x.x.x IP address on the LCD, then exit."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lcd import LcdDisplay


def get_152_ip() -> str:
    """Retrieve the 152.x.x.x IP address using hostname -I, or return an error message."""
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=5
        )
        print(f"[DEBUG] hostname -I output: {result.stdout.strip()}")

        if result.returncode != 0:
            print(f"[DEBUG] hostname -I failed with code {result.returncode}")
            return "Error: hostname cmd"

        # Parse all IPs from the output
        ips = result.stdout.strip().split()
        print(f"[DEBUG] Found {len(ips)} IP(s): {ips}")

        # Filter for IPs starting with 152.
        for ip in ips:
            print(f"[DEBUG] Checking IP: {ip}")
            if ip.startswith("152."):
                print(f"[DEBUG] Found 152.x IP: {ip}")
                return ip

        print("[DEBUG] No 152.x IP found")
        return "No 152.x IP"
    except subprocess.TimeoutExpired:
        print("[DEBUG] hostname command timed out")
        return "Error: timeout"
    except Exception as e:
        error_msg = f"Error: {str(e)[:15]}"
        print(f"[DEBUG] Exception: {e}")
        return error_msg


if __name__ == "__main__":
    print("[DEBUG] Initializing LCD display")
    lcd = LcdDisplay()
    print("[DEBUG] LCD initialized")

    try:
        print("[DEBUG] Clearing display")
        lcd.clear()
        ip_addr = get_152_ip()
        print(f"[DEBUG] Displaying on LCD: '{ip_addr}'")
        lcd.write_row(0, ip_addr)
        print("[DEBUG] IP written to display")
    finally:
        print("[DEBUG] Done")
