#!/usr/bin/env python3
"""
Send a fill command to the ESP32, capture debug output, save CSV + plot.

Usage:
    python3 capture_fill_debug.py --weight 100

Install dependencies:
    pip install pyserial matplotlib
"""

import argparse
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed. Run: pip install pyserial")
    sys.exit(1)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    print("WARNING: matplotlib not installed. CSV saved but no plot.")
    print("Install with: pip install matplotlib")
    HAS_MATPLOTLIB = False

PUMP_INDEX  = 2
OUTPUT_DIR  = Path(__file__).parent.parent / "DebugScaleFilling"
OUTPUT_DIR.mkdir(exist_ok=True)

# Match a data row: starts with digits then a comma (t_ms,raw,...)
DATA_ROW = re.compile(r"^\s*\d+,")


def save_csv(csv_path: Path, rows: list):
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["t_ms", "raw", "filtered_g", "delta_g"])
        writer.writerows(rows)
    print(f"  CSV saved:   {csv_path}  ({len(rows)} samples)")


def save_plot(csv_path: Path, target: float):
    if not HAS_MATPLOTLIB:
        return
    try:
        t_ms, raw_vals, filtered_g, delta_g = [], [], [], []
        with open(csv_path) as f:
            for row in csv.DictReader(f):
                t_ms.append(int(row["t_ms"]))
                raw_vals.append(int(row["raw"]))
                filtered_g.append(float(row["filtered_g"]))
                delta_g.append(float(row["delta_g"]))

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
        fig.suptitle(f"Fill debug — pump {PUMP_INDEX}, target {target}g", fontsize=13)

        ax1.plot(t_ms, filtered_g, color="steelblue", linewidth=1.5, label="filtered weight (g)")
        ax1.set_ylabel("Absolute weight (g)")
        ax1.legend(); ax1.grid(True, alpha=0.3)

        ax2.plot(t_ms, delta_g, color="tomato", linewidth=1.5, label="dispensed delta (g)")
        ax2.axhline(-target,        color="gray",   linestyle="--", linewidth=1, label=f"target −{target}g")
        ax2.axhline(-target * 0.92, color="orange", linestyle=":",  linewidth=1, label=f"stop at −{target*0.92:.1f}g")
        ax2.set_xlabel("Time (ms)"); ax2.set_ylabel("Weight delta (g)")
        ax2.legend(); ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        png = csv_path.with_suffix(".png")
        plt.savefig(png, dpi=150); plt.close()
        print(f"  Plot saved:  {png}")
    except Exception as e:
        print(f"  Plot failed: {e}")


def run(weight: float, port: str, baud: int):
    print(f"Connecting to {port} ...")
    ser = serial.Serial(port, baud, timeout=1)

    # Wait for ESP32 to finish booting after port-open reset
    print("Waiting for ESP32 to boot (3s)...")
    time.sleep(3)
    ser.reset_input_buffer()   # discard boot messages

    cmd = f"G4 I{PUMP_INDEX} W{weight}\r"
    print(f"Sending: {cmd.strip()}")
    ser.write(cmd.encode())

    capturing  = False
    rows       = []
    csv_path   = None
    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("Waiting for [FILL_START] ...\n")
    try:
        while True:
            raw_line = ser.readline()
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace")
            print(line, end="")

            if "[FILL_START]" in line:
                capturing = True
                rows      = []
                csv_path  = OUTPUT_DIR / f"fill_pump{PUMP_INDEX}_{weight}g_{ts}.csv"
                print(f"\n  [capture] Recording to {csv_path}")

            elif capturing and DATA_ROW.match(line):
                # Strip any log prefix (INFO - ) before the digits
                clean = re.sub(r"^.*?(\d)", r"\1", line).strip()
                parts = clean.split(",")
                if len(parts) == 4:
                    rows.append(parts)

            elif "[FILL_END]" in line:
                capturing = False
                if rows and csv_path:
                    save_csv(csv_path, rows)
                    save_plot(csv_path, weight)
                print("\nDone.")
                break

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        # Safety net: save whatever was captured if Ctrl+C hit mid-fill
        if rows and csv_path and not csv_path.exists():
            print("  Saving partial data...")
            save_csv(csv_path, rows)
            save_plot(csv_path, weight)
        ser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BarBot fill — pump 2")
    parser.add_argument("--weight", type=float, default=100.0,
                        help="Target weight to dispense in grams (default: 100)")
    parser.add_argument("--port",   default="/dev/ttyACM0")
    parser.add_argument("--baud",   type=int, default=115200)
    args = parser.parse_args()
    run(args.weight, args.port, args.baud)
