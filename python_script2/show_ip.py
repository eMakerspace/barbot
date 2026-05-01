#!/usr/bin/env python3
"""Display the 152.x.x.x IP address on the LCD, then exit."""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lcd import LcdDisplay


def get_152_ip() -> str:
    """Retrieve the 152.x.x.x IP address, or return an error message."""
    try:
        # Get all addresses for this hostname
        hostname = socket.gethostname()
        print(f"[DEBUG] Hostname: {hostname}")
        addresses = socket.getaddrinfo(hostname, None)
        print(f"[DEBUG] Found {len(addresses)} address(es)")

        # Filter for IPv4 addresses starting with 152.
        for addr_info in addresses:
            ip = addr_info[4][0]
            print(f"[DEBUG] Checking IP: {ip}")
            if ip.startswith("152."):
                print(f"[DEBUG] Found 152.x IP: {ip}")
                return ip

        print("[DEBUG] No 152.x IP found")
        return "No 152.x IP"
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
        print("[DEBUG] Turning off backlight")
        lcd.backlight = False
        print("[DEBUG] Done")
