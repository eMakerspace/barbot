#!/usr/bin/env python3
"""BarBot Web Control Panel — Flask + SocketIO (threading mode, no monkey-patch)."""

import json
import socket as _socket
import threading
import time
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO, emit

HW_CONFIG_PATH         = Path(__file__).parent.parent / "python_script2" / "config" / "hardware_config.json"
SLOTS_CONFIG_PATH      = Path(__file__).parent.parent / "python_script2" / "config" / "slots_config.json"
SAVED_DRINKS_PATH      = Path(__file__).parent.parent / "python_script2" / "config" / "saved_drinks.json"

SPIRIT_SLOT_NAMES = [f"Slot_{i}" for i in range(1, 9)]
MIXER_SLOT_NAMES  = [f"Slot_{c}" for c in "ABCD"]

app = Flask(__name__)
app.config["SECRET_KEY"] = "barbot-secret"
# threading mode: no monkey-patch, works reliably with pyserial
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── IPC socket state ──────────────────────────────────────────────────────────
# barbot_web.py never opens the serial port directly.  Instead it connects to
# the Unix socket that main.py (hardware.py) exposes at SOCKET_PATH.

_SOCKET_PATH = "/tmp/barbot.sock"
_sock: "_socket.socket | None" = None
_sock_lock = threading.Lock()


def _emit_all(event: str, payload: dict):
    """Emit an event to all connected web clients."""
    socketio.emit(event, payload, namespace="/")


def _ipc_connected() -> bool:
    with _sock_lock:
        return _sock is not None


def _connect_ipc() -> bool:
    """Try to open a connection to the barbot IPC socket.  Returns True on success."""
    global _sock
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(1.0)          # so recv() doesn't block forever in reader
        s.connect(_SOCKET_PATH)
        with _sock_lock:
            _sock = s
        return True
    except OSError as e:
        print(f"[ipc] connect failed: {e}")
        return False


def _disconnect_ipc():
    global _sock
    with _sock_lock:
        s, _sock = _sock, None
    if s:
        try:
            s.close()
        except OSError:
            pass


def serial_reader():
    """Background thread: read lines from the IPC socket and push to browser.

    Auto-reconnects every 2 s when disconnected so the web panel comes alive
    as soon as main.py starts — no manual intervention needed.
    """
    global _sock
    buf = b""
    while True:
        with _sock_lock:
            sock = _sock

        if sock is None:
            buf = b""
            if _connect_ipc():
                _emit_all("serial_out", {"data": f"[connected to barbot IPC at {_SOCKET_PATH}]"})
                _emit_all("status", {"connected": True, "port": _SOCKET_PATH})
            else:
                time.sleep(2)
            continue

        try:
            chunk = sock.recv(1024)
        except _socket.timeout:
            continue   # no data yet — loop back
        except OSError as e:
            with _sock_lock:
                if _sock is sock:
                    _sock = None
            _emit_all("serial_out", {"data": f"[IPC connection lost: {e}]"})
            _emit_all("status", {"connected": False})
            buf = b""
            time.sleep(2)
            continue

        if not chunk:
            # Server closed the connection
            with _sock_lock:
                if _sock is sock:
                    _sock = None
            _emit_all("serial_out", {"data": "[IPC disconnected — main.py stopped?]"})
            _emit_all("status", {"connected": False})
            buf = b""
            time.sleep(2)
            continue

        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                _emit_all("serial_out", {"data": text})


# Start reader/auto-reconnect thread once at startup
_reader = threading.Thread(target=serial_reader, daemon=True)
_reader.start()


def autoconnect_from_config():
    # Connection is now handled by the serial_reader thread automatically.
    pass


autoconnect_from_config()


def send_gcode(cmd: str):
    global _sock
    with _sock_lock:
        sock = _sock
    if sock is None:
        _emit_all("serial_out", {"data": "[not connected — main.py not running?]"})
        return False
    try:
        sock.sendall((cmd.strip() + "\n").encode())
        _emit_all("serial_out", {"data": f"> {cmd.strip()}"})
        return True
    except OSError as e:
        with _sock_lock:
            if _sock is sock:
                _sock = None
        _emit_all("serial_out", {"data": f"[send error] {e}"})
        return False


# ── SocketIO events ───────────────────────────────────────────────────────────

@socketio.on("connect")
def handle_socket_connect():
    """Push current IPC connection status to a newly connected browser page."""
    conn = _ipc_connected()
    emit("status", {"connected": conn, "port": _SOCKET_PATH if conn else None})


@socketio.on("list_ports")
def handle_list_ports():
    emit("ports", {"ports": []})   # not applicable in IPC mode


@socketio.on("connect_serial")
def handle_connect(data):
    if _ipc_connected():
        emit("status", {"connected": True, "port": _SOCKET_PATH})
        return
    if _connect_ipc():
        emit("status", {"connected": True, "port": _SOCKET_PATH})
        emit("serial_out", {"data": f"[connected to barbot IPC at {_SOCKET_PATH}]"})
    else:
        emit("status", {"connected": False, "error": "Cannot connect — is main.py running?"})
        emit("serial_out", {"data": "[error] Cannot connect to barbot IPC — is main.py running?"})


@socketio.on("disconnect_serial")
def handle_disconnect_serial():
    _disconnect_ipc()
    emit("status", {"connected": False})
    emit("serial_out", {"data": "[disconnected from barbot IPC]"})


@socketio.on("send_gcode")
def handle_gcode(data):
    send_gcode(data.get("cmd", ""))


# ── SocketIO: status query + drink maker ──────────────────────────────────────

@socketio.on("get_status")
def handle_get_status():
    conn = _ipc_connected()
    emit("status", {"connected": conn, "port": _SOCKET_PATH if conn else None})


@socketio.on("make_drink_debug")
def handle_make_drink_debug(data):
    """Translate a drink spec into G-code and queue it over serial."""
    try:
        hw  = json.loads(HW_CONFIG_PATH.read_text())
        cfg = json.loads(SLOTS_CONFIG_PATH.read_text())
    except Exception as e:
        emit("serial_out", {"data": f"[shop] config error: {e}"})
        return

    slot_positions     = hw.get("slot_positions", {})
    slot_mapping       = cfg.get("slot_mapping", {})
    ingredient_to_slot = {v: k for k, v in slot_mapping.items() if v}

    optic       = hw.get("spirit_optic", {})
    pour_angle  = optic.get("pour_angle", 90)
    close_angle = optic.get("close_angle", 180)
    pour_ms     = optic.get("pour_duration_ms", 1500)
    settle_ms   = optic.get("settle_duration_ms", 500)

    x_axis    = hw.get("x_axis", {})
    accel     = x_axis.get("accel")
    max_speed = x_axis.get("max_speed")
    tubing_comp = float(hw.get("pump_tubing_compensation_g", 0.0))

    def move_cmd(pos):
        cmd = f"G0 X{pos}"
        if accel:     cmd += f" A{accel}"
        if max_speed: cmd += f" S{max_speed}"
        return cmd

    name    = data.get("name", "Custom")
    spirits = data.get("spirits", [])
    mixers  = data.get("mixers", [])

    emit("serial_out", {"data": f"[shop] >>> Making: {name}"})

    for s in spirits:
        slug  = s.get("slug", "")
        pours = int(s.get("pours", 2))
        slot  = ingredient_to_slot.get(slug)
        pos   = slot_positions.get(slot) if slot else None
        if pos is None:
            emit("serial_out", {"data": f"[shop] SKIP spirit {slug!r} – not loaded"})
            continue
        forbidden = next((z.get("label","forbidden zone") for z in hw.get("forbidden_servo_zones",[])
                          if z.get("min",0) <= pos <= z.get("max",0)), None)
        if forbidden:
            emit("serial_out", {"data": f"[shop] BLOCKED: {slug} at {slot} is in forbidden zone ({forbidden})"})
            continue
        emit("serial_out", {"data": f"[shop] Spirit {slug} → {slot} ×{pours}"})
        send_gcode(move_cmd(pos))
        for i in range(pours):
            send_gcode(f"G1 Z{pour_angle}")
            send_gcode(f"T0 D{pour_ms}")
            send_gcode(f"G1 Z{close_angle}")
            if i < pours - 1:
                send_gcode(f"T0 D{settle_ms}")
        send_gcode("G3")  # sync barrier — executes after all pours complete

    for m in mixers:
        slug = m.get("slug", "")
        ml   = float(m.get("ml", 150))
        slot = ingredient_to_slot.get(slug)
        pos  = slot_positions.get(slot) if slot else None
        if pos is None:
            emit("serial_out", {"data": f"[shop] SKIP mixer {slug!r} – not loaded"})
            continue
        try:
            pump_idx = MIXER_SLOT_NAMES.index(slot)
        except ValueError:
            emit("serial_out", {"data": f"[shop] SKIP {slug!r} – {slot} is not a mixer slot"})
            continue
        comp_str = f" (+{tubing_comp:.1f}g comp)" if tubing_comp else ""
        emit("serial_out", {"data": f"[shop] Mixer {slug} → {slot}/pump{pump_idx} {ml:.0f}ml{comp_str}"})
        send_gcode(move_cmd(pos))
        send_gcode("G3.1")
        target_g = ml + tubing_comp
        send_gcode(f"G4 I{pump_idx} W{target_g:.1f}")

    emit("serial_out", {"data": "[shop] Drink queued — enjoy! 🍹"})


# ── REST helpers ─────────────────────────────────────────────────────────────

@app.route("/api/slots")
def api_slots():
    try:
        cfg = json.loads(HW_CONFIG_PATH.read_text())
        return jsonify({
            "slots": cfg.get("slot_positions", {}),
            "x_max": cfg.get("x_axis", {}).get("max_steps", 6000),
        })
    except Exception as e:
        return jsonify({"slots": {}, "x_max": 6000, "error": str(e)})


@app.route("/api/hardware_config", methods=["GET"])
def api_hardware_config_get():
    try:
        return jsonify(json.loads(HW_CONFIG_PATH.read_text()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/hardware_config", methods=["POST"])
def api_hardware_config_post():
    try:
        cfg = json.loads(HW_CONFIG_PATH.read_text())
        patch = request.get_json(force=True) or {}
        # Deep-merge one level: only update keys that were sent
        for section, values in patch.items():
            if isinstance(values, dict) and isinstance(cfg.get(section), dict):
                cfg[section].update(values)
            else:
                cfg[section] = values
        HW_CONFIG_PATH.write_text(json.dumps(cfg, indent=4))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ingredients")
def api_ingredients():
    try:
        slot_mapping = json.loads(SLOTS_CONFIG_PATH.read_text()).get("slot_mapping", {})
        spirits = {v: k for k, v in slot_mapping.items() if v and k in SPIRIT_SLOT_NAMES}
        mixers  = {v: k for k, v in slot_mapping.items() if v and k in MIXER_SLOT_NAMES}
        return jsonify({"spirits": spirits, "mixers": mixers, "slot_mapping": slot_mapping})
    except Exception as e:
        return jsonify({"spirits": {}, "mixers": {}, "slot_mapping": {}, "error": str(e)})


def _load_saved_drinks() -> list:
    try:
        return json.loads(SAVED_DRINKS_PATH.read_text()).get("recipes", [])
    except FileNotFoundError:
        return []


def _save_saved_drinks(recipes: list):
    SAVED_DRINKS_PATH.write_text(json.dumps({"recipes": recipes}, indent=4))


@app.route("/api/saved_drinks", methods=["GET"])
def api_saved_drinks_get():
    return jsonify({"recipes": _load_saved_drinks()})


@app.route("/api/saved_drinks", methods=["POST"])
def api_saved_drinks_post():
    recipe = request.get_json(force=True) or {}
    name = (recipe.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    recipes = _load_saved_drinks()
    # Overwrite if name already exists
    recipes = [r for r in recipes if r.get("name") != name]
    recipes.append(recipe)
    _save_saved_drinks(recipes)
    return jsonify({"ok": True})


@app.route("/api/saved_drinks/<path:name>", methods=["DELETE"])
def api_saved_drinks_delete(name):
    recipes = [r for r in _load_saved_drinks() if r.get("name") != name]
    _save_saved_drinks(recipes)
    return jsonify({"ok": True})


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BarBot Control Panel</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<style>
  body { background: #0f172a; color: #e2e8f0; font-family: system-ui, sans-serif; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; }
  .section-title { color: #f59e0b; font-size: 0.7rem; letter-spacing: 0.15em; text-transform: uppercase; font-weight: 700; }
  input[type=range] { accent-color: #f59e0b; }
  input[type=number], input[type=text], select {
    background: #0f172a; border: 1px solid #334155; color: #e2e8f0;
    border-radius: 6px; padding: 0.4rem 0.6rem; width: 100%; font-size: 0.85rem;
  }
  input:focus, select:focus { outline: 2px solid #f59e0b; border-color: transparent; }
  .btn { border-radius: 8px; padding: 0.5rem 1rem; font-weight: 600; font-size: 0.85rem;
         cursor: pointer; transition: all 0.15s; display:inline-flex; align-items:center;
         gap:6px; border: none; justify-content: center; }
  .btn:active { transform: scale(0.96); }
  .btn-amber { background: #f59e0b; color: #0f172a; }
  .btn-amber:hover { background: #fbbf24; }
  .btn-blue  { background: #3b82f6; color: #fff; }
  .btn-blue:hover  { background: #60a5fa; }
  .btn-green { background: #22c55e; color: #0f172a; }
  .btn-green:hover { background: #4ade80; }
  .btn-red   { background: #ef4444; color: #fff; }
  .btn-red:hover   { background: #f87171; }
  .btn-slate { background: #334155; color: #e2e8f0; }
  .btn-slate:hover { background: #475569; }
  .btn-sm { padding: 0.3rem 0.7rem; font-size: 0.8rem; }

  #estop {
    background: radial-gradient(circle at 40% 35%, #ff6b6b, #b91c1c);
    box-shadow: 0 0 0 4px #450a0a, 0 8px 32px rgba(239,68,68,0.5);
    border-radius: 50%; width: 90px; height: 90px;
    font-size: 0.72rem; font-weight: 900; letter-spacing: 0.05em;
    display:flex; align-items:center; justify-content:center; flex-direction:column;
    cursor:pointer; transition: transform 0.1s; color:#fff; border: none;
  }
  #estop:hover { transform: scale(1.06); box-shadow: 0 0 0 4px #7f1d1d, 0 12px 40px rgba(239,68,68,0.7); }
  #estop:active { transform: scale(0.92); }

  #console { font-family: 'Courier New', monospace; font-size: 0.78rem;
             background: #020617; border: 1px solid #1e3a5f; border-radius: 8px;
             height: 240px; overflow-y: auto; padding: 10px; }
  .c-cmd  { color: #f59e0b; }
  .c-info { color: #38bdf8; }
  .c-err  { color: #ef4444; }
  .c-out  { color: #94a3b8; }

  .dot { width: 10px; height: 10px; border-radius: 50%; display:inline-block; flex-shrink:0; }
  .dot-red   { background: #ef4444; box-shadow: 0 0 6px #ef4444; }
  .dot-green { background: #22c55e; box-shadow: 0 0 6px #22c55e; animation: blink 1.5s infinite; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.4} }

  .pump-card { background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 10px; }
  .val-badge { background: #334155; border-radius: 5px; padding: 1px 8px;
               font-size: 0.8rem; font-family: monospace; min-width: 52px; text-align:center; display:inline-block; }
  @keyframes pulse-red { 0%,100%{box-shadow:0 0 0 0 #ef444466,inset 0 0 0 0 #ef444411} 50%{box-shadow:0 0 0 8px #ef444400,inset 0 0 20px #ef444422} }
  #servo-card.servo-locked { border-color: #ef4444 !important; animation: pulse-red 1.4s infinite; }
</style>
</head>
<body class="min-h-screen p-4">

<!-- Nav -->
<nav style="display:flex;gap:4px;background:#1e293b;border:1px solid #334155;border-radius:12px;padding:4px;margin-bottom:16px;">
  <a href="/"     style="flex:1;text-align:center;padding:8px 0;border-radius:8px;font-size:0.85rem;font-weight:700;text-decoration:none;background:#f59e0b;color:#0f172a;">⚙️ Control Panel</a>
  <a href="/shop" style="flex:1;text-align:center;padding:8px 0;border-radius:8px;font-size:0.85rem;font-weight:600;text-decoration:none;color:#94a3b8;">🍹 Drink Menu</a>
</nav>

<div class="flex items-center justify-between mb-4">
  <div class="flex items-center gap-3">
    <span class="text-3xl">🍹</span>
    <div>
      <h1 class="text-xl font-bold text-amber-400">BarBot</h1>
      <p class="text-xs text-slate-400">Control Panel</p>
    </div>
  </div>
  <div class="flex items-center gap-2 text-sm">
    <span class="dot dot-red" id="status-dot"></span>
    <span id="status-text" class="text-slate-400">Disconnected</span>
  </div>
</div>

<!-- Connection bar -->
<div class="card p-3 mb-4 flex flex-wrap gap-2 items-center">
  <div class="flex-1 min-w-0">
    <label class="section-title mb-1 block">Barbot IPC</label>
    <span class="text-xs text-slate-400 font-mono">/tmp/barbot.sock</span>
    <span class="text-xs text-slate-500 ml-2">(owned by main.py)</span>
  </div>
  <button id="connect-btn" class="btn btn-green" onclick="toggleConnect()">
    <i class="fa fa-plug"></i> Connect
  </button>
</div>

<div class="grid grid-cols-1 lg:grid-cols-3 gap-4">

  <!-- LEFT: Safety + Stepper -->
  <div class="flex flex-col gap-4">

    <div class="card p-4 flex flex-col items-center gap-3">
      <p class="section-title">Safety & Status</p>
      <div class="w-full">
        <p class="text-xs text-slate-400 mb-2 text-center">Cup Presence</p>
        <div style="display:flex;align-items:center;justify-content:center;gap:8px;padding:10px;background:#0f172a;border:1px solid #334155;border-radius:8px;">
          <span class="dot dot-red" id="cup-dot"></span>
          <span id="cup-status" style="font-size:0.9rem;font-weight:600;color:#94a3b8">—</span>
        </div>
      </div>
      <button id="estop" onclick="gcode('M0.1',this)" style="margin-top:4px;">
        <i class="fa fa-hand text-lg mb-1"></i>
        <span>E-STOP</span>
      </button>
      <div class="flex gap-2 w-full">
        <button class="btn btn-slate flex-1 btn-sm" onclick="gcode('M0',this)">
          <i class="fa fa-stop"></i> Graceful
        </button>
        <button class="btn btn-green flex-1 btn-sm" onclick="gcode('M1',this)">
          <i class="fa fa-play"></i> Continue
        </button>
      </div>
    </div>

    <div class="card p-4 flex flex-col gap-3">
      <p class="section-title">Stepper Motor</p>
      <button class="btn btn-amber w-full" onclick="gcode('G28',this);updateServoLock(0);">
        <i class="fa fa-house"></i> Home (G28)
      </button>
      <div>
        <div class="flex justify-between items-center mb-1">
          <label class="text-xs text-slate-300">Position (0.0 – 1.0)</label>
          <span class="val-badge" id="range-val">0.50</span>
        </div>
        <input type="range" id="range-slider" min="0" max="100" value="50" step="1" class="w-full"
          oninput="document.getElementById('range-val').textContent=((100-this.value)/100).toFixed(2)">
        <button class="btn btn-blue w-full mt-2 btn-sm" onclick="sendRangeMove(this)">
          <i class="fa fa-arrow-right"></i> Move to Position (G0.1)
        </button>
      </div>
      <div class="grid grid-cols-2 gap-2">
        <div>
          <label class="text-xs text-slate-400 block mb-1">Accel A (steps/s²)</label>
          <input type="number" id="stepper-accel" placeholder="default" min="50" max="50000">
        </div>
        <div>
          <label class="text-xs text-slate-400 block mb-1">Speed S (steps/s)</label>
          <input type="number" id="stepper-speed" placeholder="default" min="20" max="30000">
        </div>
      </div>
      <button class="btn btn-slate btn-sm w-full" onclick="saveStepperConfig(this)">
        <i class="fa fa-floppy-disk"></i> Save Accel & Speed
      </button>
      <div>
        <label class="text-xs text-slate-400 block mb-1">Absolute step (G0 X…)</label>
        <div class="flex gap-2">
          <input type="number" id="abs-pos" placeholder="0" class="flex-1">
          <button class="btn btn-blue btn-sm" onclick="sendAbsMove(this)">Go</button>
        </div>
      </div>
      <div class="border-t border-slate-700 pt-3">
        <div class="flex items-center justify-between mb-1">
          <p class="text-xs text-slate-400">Spirit Slots</p>
          <button class="btn btn-slate btn-sm py-0 px-2 text-xs" onclick="fetchSlots()">
            <i class="fa fa-rotate"></i>
          </button>
        </div>
        <div id="spirit-slots" class="grid grid-cols-4 gap-1 mb-3">
          <span class="text-xs text-slate-500 col-span-4">Loading…</span>
        </div>
        <p class="text-xs text-slate-400 mb-1">Mixer Slots</p>
        <div id="mixer-slots" class="grid grid-cols-4 gap-1">
          <span class="text-xs text-slate-500 col-span-4">Loading…</span>
        </div>
      </div>
    </div>
  </div>

  <!-- CENTER: Servo + Pumps + Wait -->
  <div class="flex flex-col gap-4">

    <div class="card p-4 flex flex-col gap-3" id="servo-card">
      <p class="section-title">Servo Motor</p>
      <div>
        <div class="flex justify-between items-center mb-1">
          <label class="text-xs text-slate-300">Angle (0 – 180°)</label>
          <span class="val-badge" id="servo-val">90°</span>
        </div>
        <input type="range" id="servo-slider" min="0" max="180" value="90" step="1" class="w-full"
          oninput="document.getElementById('servo-val').textContent=this.value+'°'"
          onchange="servoSliderChanged(this.value, this)">
        <button id="servo-set-btn" class="btn btn-blue w-full mt-2 btn-sm"
          onclick="gcode('G1 Z'+document.getElementById('servo-slider').value,this)">
          <i class="fa fa-rotate"></i> Set Angle (G1)
        </button>
      </div>
      <div class="border-t border-slate-700 pt-3 grid grid-cols-2 gap-3">
        <div>
          <label class="text-xs text-slate-400 block mb-1">Open angle °</label>
          <input type="number" id="servo-open" value="90" min="0" max="180" class="mb-2">
          <button id="servo-open-btn" class="btn btn-green w-full btn-sm"
            onclick="servoPreset('open',this)">
            <i class="fa fa-lock-open"></i> Open
          </button>
        </div>
        <div>
          <label class="text-xs text-slate-400 block mb-1">Close angle °</label>
          <input type="number" id="servo-close" value="180" min="0" max="180" class="mb-2">
          <button class="btn btn-slate w-full btn-sm"
            onclick="servoPreset('close',this)">
            <i class="fa fa-lock"></i> Close
          </button>
        </div>
      </div>
      <div class="border-t border-slate-700 pt-3 grid grid-cols-2 gap-2">
        <div>
          <label class="text-xs text-slate-400 block mb-1">Pour duration (ms)</label>
          <input type="number" id="pour-duration" min="100" max="10000" step="50">
        </div>
        <div>
          <label class="text-xs text-slate-400 block mb-1">Settle duration (ms)</label>
          <input type="number" id="settle-duration" min="0" max="5000" step="50">
        </div>
      </div>
      <button class="btn btn-slate btn-sm w-full" onclick="saveServoConfig(this)">
        <i class="fa fa-floppy-disk"></i> Save Servo Config
      </button>
    </div>

    <div class="card p-4 flex flex-col gap-3">
      <p class="section-title">Pumps</p>
      <div class="grid grid-cols-2 gap-2">
        <div class="pump-card">
          <p class="text-xs font-bold text-amber-400 mb-1">Pump 0</p>
          <input type="number" id="pump0-dur" value="500" min="0" max="30000" class="mb-1">
          <div class="flex gap-1 mt-1">
            <button class="btn btn-blue btn-sm flex-1" onclick="sendPump(0,false,this)">Run</button>
            <button class="btn btn-amber btn-sm flex-1" onclick="sendPump(0,true,this)">Wait</button>
          </div>
        </div>
        <div class="pump-card">
          <p class="text-xs font-bold text-amber-400 mb-1">Pump 1</p>
          <input type="number" id="pump1-dur" value="500" min="0" max="30000" class="mb-1">
          <div class="flex gap-1 mt-1">
            <button class="btn btn-blue btn-sm flex-1" onclick="sendPump(1,false,this)">Run</button>
            <button class="btn btn-amber btn-sm flex-1" onclick="sendPump(1,true,this)">Wait</button>
          </div>
        </div>
        <div class="pump-card">
          <p class="text-xs font-bold text-amber-400 mb-1">Pump 2</p>
          <input type="number" id="pump2-dur" value="500" min="0" max="30000" class="mb-1">
          <div class="flex gap-1 mt-1">
            <button class="btn btn-blue btn-sm flex-1" onclick="sendPump(2,false,this)">Run</button>
            <button class="btn btn-amber btn-sm flex-1" onclick="sendPump(2,true,this)">Wait</button>
          </div>
        </div>
        <div class="pump-card">
          <p class="text-xs font-bold text-amber-400 mb-1">Pump 3</p>
          <input type="number" id="pump3-dur" value="500" min="0" max="30000" class="mb-1">
          <div class="flex gap-1 mt-1">
            <button class="btn btn-blue btn-sm flex-1" onclick="sendPump(3,false,this)">Run</button>
            <button class="btn btn-amber btn-sm flex-1" onclick="sendPump(3,true,this)">Wait</button>
          </div>
        </div>
      </div>
      <button class="btn btn-red btn-sm w-full" onclick="stopAllPumps(this)">
        <i class="fa fa-stop"></i> Stop All Pumps (D0)
      </button>
    </div>

    <div class="card p-4 flex flex-col gap-2">
      <p class="section-title">Wait / Delay</p>
      <div class="flex gap-2">
        <input type="number" id="wait-ms" value="1000" min="1" max="60000" class="flex-1" placeholder="ms">
        <button class="btn btn-slate btn-sm"
          onclick="gcode('T0 D'+document.getElementById('wait-ms').value,this)">
          <i class="fa fa-clock"></i> Wait (T0)
        </button>
      </div>
    </div>
  </div>

  <!-- RIGHT: Scale + Console -->
  <div class="flex flex-col gap-4">

    <div class="card p-4 flex flex-col gap-3">
      <p class="section-title">Scale (HX711)</p>
      <div id="weight-display" style="background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px 16px;text-align:center;">
        <span style="font-size:0.7rem;color:#64748b;letter-spacing:0.1em;text-transform:uppercase;">Weight</span>
        <div id="weight-value" style="font-size:2rem;font-weight:700;font-family:monospace;color:#f59e0b;line-height:1.1;">—</div>
        <div id="weight-raw" style="font-size:0.7rem;color:#475569;margin-top:2px;"></div>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-blue flex-1 btn-sm" onclick="gcode('G3',this)">
          <i class="fa fa-weight-hanging"></i> Read (G3)
        </button>
        <button class="btn btn-slate flex-1 btn-sm" onclick="gcode('G3.1',this)">
          <i class="fa fa-circle-dot"></i> Tare (G3.1)
        </button>
      </div>
      <div>
        <label class="text-xs text-slate-400 block mb-1">Calibrate — known weight (g)</label>
        <div class="flex gap-2">
          <input type="number" id="cal-grams" placeholder="100" min="1" class="flex-1">
          <button class="btn btn-amber btn-sm" onclick="sendCalibrate(this)">Calibrate (G3.2)</button>
        </div>
      </div>
      <div class="border-t border-slate-700 pt-3">
        <p class="text-xs font-semibold text-slate-300 mb-2">Fill by Weight</p>
        <div class="grid grid-cols-2 gap-2 mb-2">
          <div>
            <label class="text-xs text-slate-400 block mb-1">Pump #</label>
            <input type="number" id="fill-pump" value="0" min="0" max="3">
          </div>
          <div>
            <label class="text-xs text-slate-400 block mb-1">Target (g)</label>
            <input type="number" id="fill-grams" value="50" min="1">
          </div>
        </div>
        <button class="btn btn-green w-full btn-sm" onclick="sendFill(this)">
          <i class="fa fa-fill"></i> Fill to Weight (G4)
        </button>
      </div>
    </div>

    <div class="card p-4 flex flex-col gap-2 flex-1">
      <div class="flex items-center justify-between">
        <p class="section-title">Serial Console</p>
        <button class="btn btn-slate btn-sm" onclick="document.getElementById('console').innerHTML=''">
          <i class="fa fa-trash"></i> Clear
        </button>
      </div>
      <div id="console"></div>
      <div class="flex gap-2 mt-1">
        <input type="text" id="raw-cmd" placeholder="Raw G-code (↑↓ for history)…" class="flex-1"
               onkeydown="handleRawKey(event)">
        <button class="btn btn-amber btn-sm" onclick="sendRaw()">Send</button>
      </div>
    </div>
  </div>
</div>

<script>
// Force polling — Werkzeug dev server cannot handle WebSocket upgrades
const socket = io({ transports: ['polling'] });
let connected = false;
let cmdHistory = [];
let histIdx = -1;
try { cmdHistory = JSON.parse(sessionStorage.cmdHistory || '[]'); } catch(e) {}

let forbiddenZones = [];
let xMax = 6000;
let currentPos = 0;

function updateServoLock(pos) {
  currentPos = pos;
  // Forbidden zone enforcement is now handled by ESP32 firmware
  // This function just tracks the stepper position for reference
}

function servoSliderChanged(val, btn) {
  // Only send once on release (onchange), not on every input
  // Forbidden zone checking is now handled at ESP32 level
  gcode('G1 Z' + val, btn);
}

socket.on('connect', () => { fetchSlots(); socket.emit('get_status'); loadHardwareConfig(); });
socket.on('disconnect', () => {
  document.getElementById('status-dot').className = 'dot dot-red';
  document.getElementById('status-text').textContent = 'Socket disconnected — reload page';
});

socket.on('ports', data => {
  const sel = document.getElementById('port-select');
  sel.innerHTML = data.ports.length
    ? data.ports.map(p => `<option value="${p}">${p}</option>`).join('')
    : '<option value="">— No ports found —</option>';
});

socket.on('status', data => {
  connected = data.connected;
  document.getElementById('status-dot').className = 'dot ' + (connected ? 'dot-green' : 'dot-red');
  document.getElementById('status-text').textContent = connected
    ? `Connected · ${data.port}`
    : (data.error ? `Error: ${data.error}` : 'Disconnected');
  const btn = document.getElementById('connect-btn');
  btn.className = 'btn ' + (connected ? 'btn-red' : 'btn-green');
  btn.innerHTML = connected
    ? '<i class="fa fa-plug-circle-xmark"></i> Disconnect'
    : '<i class="fa fa-plug"></i> Connect';
});

socket.on('serial_out', data => {
  const el = document.getElementById('console');
  const line = document.createElement('div');
  const text = data.data;
  if      (text.startsWith('>'))        line.className = 'c-cmd';
  else if (text.startsWith('['))        line.className = 'c-info';
  else if (/error|warn/i.test(text))    line.className = 'c-err';
  else                                  line.className = 'c-out';
  line.textContent = text;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;

  // Update cup presence status
  if (text.includes('[cup] PRESENT')) {
    document.getElementById('cup-dot').className = 'dot dot-green';
    document.getElementById('cup-status').textContent = 'PRESENT';
  } else if (text.includes('[cup] ABSENT')) {
    document.getElementById('cup-dot').className = 'dot dot-red';
    document.getElementById('cup-status').textContent = 'ABSENT';
  }

  // Update weight display if this line contains a scale reading
  const wm = text.match(/(-?[\d.]+)\s*g\s*\(raw:\s*(-?[\d]+)/i);
  if (wm) {
    document.getElementById('weight-value').textContent = parseFloat(wm[1]).toFixed(2) + ' g';
    document.getElementById('weight-raw').textContent = 'raw: ' + wm[2];
  } else if (/scale tared/i.test(text)) {
    document.getElementById('weight-value').textContent = '0.00 g';
    document.getElementById('weight-raw').textContent = 'tared';
  }
});

function refreshPorts() { /* IPC mode — no serial port enumeration */ }

function toggleConnect() {
  if (connected) {
    socket.emit('disconnect_serial');
  } else {
    socket.emit('connect_serial', {});
  }
}

// Flash a button green briefly to confirm the send
function flash(btn) {
  if (!btn) return;
  const orig = btn.className;
  btn.className = orig.replace(/btn-\w+/, 'btn-green') + ' scale-95';
  btn.style.transition = 'none';
  setTimeout(() => { btn.className = orig; btn.style.transition = ''; }, 350);
}

function gcode(cmd, btn) {
  if (!connected) {
    // Shake the button red to show not connected
    if (btn) {
      const orig = btn.className;
      btn.className = orig.replace(/btn-\w+/, 'btn-red');
      setTimeout(() => { btn.className = orig; }, 500);
    }
    return;
  }
  socket.emit('send_gcode', { cmd });
  flash(btn);
}

function handleRawKey(e) {
  if (e.key === 'Enter') { sendRaw(); return; }
  if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (histIdx < cmdHistory.length - 1) histIdx++;
    e.target.value = cmdHistory[cmdHistory.length - 1 - histIdx] || '';
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (histIdx > 0) { histIdx--; e.target.value = cmdHistory[cmdHistory.length - 1 - histIdx] || ''; }
    else { histIdx = -1; e.target.value = ''; }
  }
}

function sendRaw() {
  const el = document.getElementById('raw-cmd');
  const cmd = el.value.trim();
  if (!cmd) return;
  cmdHistory.push(cmd);
  sessionStorage.cmdHistory = JSON.stringify(cmdHistory.slice(-50));
  histIdx = -1;
  gcode(cmd);
  el.value = '';
}

function buildAccelSuffix() {
  const a = document.getElementById('stepper-accel').value;
  const s = document.getElementById('stepper-speed').value;
  return (a ? ` A${a}` : '') + (s ? ` S${s}` : '');
}
function sendRangeMove(btn) {
  const sliderVal = document.getElementById('range-slider').value;
  const v = ((100 - sliderVal) / 100).toFixed(2);
  const pos = Math.round((1 - sliderVal / 100) * xMax);
  updateServoLock(pos);
  gcode(`G0.1 X${v}${buildAccelSuffix()}`, btn);
}
function sendAbsMove(btn) {
  const pos = parseInt(document.getElementById('abs-pos').value);
  if (isNaN(pos)) return;
  updateServoLock(pos);
  gcode(`G0 X${pos}${buildAccelSuffix()}`, btn);
}

function sendPump(idx, wait, btn) {
  const ms = document.getElementById(`pump${idx}-dur`).value;
  gcode(`${wait ? 'G2.1' : 'G2'} I${idx} D${ms}`, btn);
}
function stopAllPumps(btn) {
  [0,1,2,3].forEach(i => gcode(`G2 I${i} D0`));
  flash(btn);
}

// ── Slots ────────────────────────────────────────────────────────────────────
const SPIRIT_SLOTS = ['Slot_1','Slot_2','Slot_3','Slot_4','Slot_5','Slot_6','Slot_7','Slot_8'];
const MIXER_SLOTS  = ['Slot_A','Slot_B','Slot_C','Slot_D'];

async function fetchSlots() {
  try {
    const data = await fetch('/api/slots').then(r => r.json());
    buildSlotButtons(data.slots || {});
  } catch(e) {
    console.error('fetchSlots failed', e);
  }
}

function buildSlotButtons(slots) {
  function makeBtn(name, pos) {
    const label = name.replace('Slot_', '');
    const posStr = pos != null ? pos : '?';
    const dimmed = pos == null ? ' opacity-40' : '';
    return `<button class="btn btn-slate btn-sm flex-col py-1 gap-0 leading-tight${dimmed}"
              style="min-width:0"
              onclick="moveToSlot('${name}',${pos ?? 'null'})">
              <span class="text-xs font-bold">${label}</span>
              <span class="text-amber-400 font-mono" style="font-size:0.65rem">${posStr}</span>
            </button>`;
  }
  document.getElementById('spirit-slots').innerHTML =
    SPIRIT_SLOTS.map(s => makeBtn(s, slots[s] ?? null)).join('');
  document.getElementById('mixer-slots').innerHTML =
    MIXER_SLOTS.map(s => makeBtn(s, slots[s] ?? null)).join('');
}

function moveToSlot(name, pos) {
  if (pos == null) {
    const el = document.getElementById('console');
    const line = document.createElement('div');
    line.className = 'c-err';
    line.textContent = `[warn] ${name} has no position saved`;
    el.appendChild(line); el.scrollTop = el.scrollHeight;
    return;
  }
  updateServoLock(pos);
  gcode(`G0 X${pos}${buildAccelSuffix()}`);
}

// ── Servo presets ─────────────────────────────────────────────────────────────
function servoPreset(which, btn) {
  // Note: forbidden zone checking is now done at ESP32 level
  // If blocked, ESP32 will send a warning that we display in console
  const angle = document.getElementById(`servo-${which}`).value;
  const slider = document.getElementById('servo-slider');
  slider.value = angle;
  document.getElementById('servo-val').textContent = angle + '°';
  gcode(`G1 Z${angle}`, btn);
}

async function loadHardwareConfig() {
  try {
    const cfg = await fetch('/api/hardware_config').then(r => r.json());
    const optic = cfg.spirit_optic || {};
    const x     = cfg.x_axis || {};
    if (optic.pour_angle      != null) document.getElementById('servo-open').value      = optic.pour_angle;
    if (optic.close_angle     != null) document.getElementById('servo-close').value     = optic.close_angle;
    if (optic.pour_duration_ms  != null) document.getElementById('pour-duration').value  = optic.pour_duration_ms;
    if (optic.settle_duration_ms != null) document.getElementById('settle-duration').value = optic.settle_duration_ms;
    if (x.accel     != null) document.getElementById('stepper-accel').value = x.accel;
    if (x.max_speed != null) document.getElementById('stepper-speed').value = x.max_speed;
    forbiddenZones = cfg.forbidden_servo_zones || [];
    xMax = x.max_steps || 6000;
    updateServoLock(currentPos);
  } catch(e) { console.error('loadHardwareConfig failed', e); }
}

async function saveConfig(patch, btn) {
  const orig = btn.className;
  try {
    const res = await fetch('/api/hardware_config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(patch),
    });
    const data = await res.json();
    btn.className = orig.replace(/btn-\w+/, data.ok ? 'btn-green' : 'btn-red');
  } catch(e) {
    btn.className = orig.replace(/btn-\w+/, 'btn-red');
  }
  setTimeout(() => { btn.className = orig; }, 1200);
}

function saveServoConfig(btn) {
  saveConfig({ spirit_optic: {
    pour_angle:         parseInt(document.getElementById('servo-open').value),
    close_angle:        parseInt(document.getElementById('servo-close').value),
    pour_duration_ms:   parseInt(document.getElementById('pour-duration').value),
    settle_duration_ms: parseInt(document.getElementById('settle-duration').value),
  }}, btn);
}

function saveStepperConfig(btn) {
  const a = document.getElementById('stepper-accel').value;
  const s = document.getElementById('stepper-speed').value;
  saveConfig({ x_axis: {
    ...(a ? { accel: parseInt(a) } : {}),
    ...(s ? { max_speed: parseInt(s) } : {}),
  }}, btn);
}

function sendCalibrate(btn) {
  const g = document.getElementById('cal-grams').value;
  if (!g) return;
  gcode(`G3.2 W${g}`, btn);
}
function sendFill(btn) {
  const pump = document.getElementById('fill-pump').value;
  const grams = document.getElementById('fill-grams').value;
  gcode(`G4 I${pump} W${grams}`, btn);
}
</script>
</body>
</html>"""


SHOP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BarBot Drink Menu</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<style>
  body { background: #0f172a; color: #e2e8f0; font-family: system-ui, sans-serif; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; }
  .section-title { color: #f59e0b; font-size: 0.7rem; letter-spacing: 0.15em; text-transform: uppercase; font-weight: 700; }
  select, input[type=number], input[type=range] { background: #0f172a; border: 1px solid #334155; color: #e2e8f0; border-radius: 6px; padding: 0.4rem 0.6rem; font-size: 0.85rem; }
  select:focus, input:focus { outline: 2px solid #f59e0b; border-color: transparent; }
  input[type=range] { accent-color: #f59e0b; width: 100%; padding: 0; }
  .btn { border-radius: 8px; padding: 0.5rem 1rem; font-weight: 600; font-size: 0.85rem;
         cursor: pointer; transition: all 0.15s; display:inline-flex; align-items:center;
         gap:6px; border: none; justify-content: center; }
  .btn:active { transform: scale(0.96); }
  .btn-amber { background: #f59e0b; color: #0f172a; }
  .btn-amber:hover { background: #fbbf24; }
  .btn-green { background: #22c55e; color: #0f172a; }
  .btn-green:hover { background: #4ade80; }
  .btn-red   { background: #ef4444; color: #fff; }
  .btn-slate { background: #334155; color: #e2e8f0; }
  .btn-slate:hover { background: #475569; }

  .drink-card {
    background: #1e293b; border: 1px solid #334155; border-radius: 12px;
    padding: 16px; display: flex; flex-direction: column; gap: 10px;
    transition: border-color 0.2s;
  }
  .drink-card:hover { border-color: #f59e0b44; }
  .drink-card.unavailable { opacity: 0.4; pointer-events: none; }
  .drink-card .emoji { font-size: 2rem; line-height: 1; }
  .drink-card h3 { font-weight: 700; font-size: 0.95rem; color: #f1f5f9; margin: 0; }
  .drink-card .ingredients { font-size: 0.75rem; color: #94a3b8; }
  .pill { display:inline-block; background:#0f172a; border:1px solid #334155;
          border-radius:99px; padding:1px 8px; font-size:0.72rem; font-family:monospace; }
  .pill.spirit { border-color:#f59e0b55; color:#f59e0b; }
  .pill.mixer  { border-color:#38bdf855; color:#38bdf8; }

  #log { font-family: 'Courier New', monospace; font-size: 0.78rem;
         background: #020617; border: 1px solid #1e3a5f; border-radius: 8px;
         height: 180px; overflow-y: auto; padding: 10px; }
  .c-cmd  { color: #f59e0b; }
  .c-info { color: #38bdf8; }
  .c-err  { color: #ef4444; }
  .c-out  { color: #94a3b8; }
  .dot { width:10px; height:10px; border-radius:50%; display:inline-block; flex-shrink:0; }
  .dot-red   { background:#ef4444; box-shadow:0 0 6px #ef4444; }
  .dot-green { background:#22c55e; box-shadow:0 0 6px #22c55e; animation:blink 1.5s infinite; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .val-badge { background:#334155; border-radius:5px; padding:1px 8px;
               font-size:0.8rem; font-family:monospace; min-width:52px; text-align:center; display:inline-block; }
  #making-banner { display:none; background:#1a2e1a; border:1px solid #22c55e44;
                   border-radius:10px; padding:10px 14px; color:#4ade80; font-size:0.85rem; }
  .builder-row { display:flex; gap:6px; align-items:center; background:#0f172a;
                 border:1px solid #334155; border-radius:8px; padding:8px 10px; }
  .builder-row select, .builder-row input[type=number] {
    background:#1e293b; border:1px solid #334155; color:#e2e8f0;
    border-radius:6px; padding:4px 6px; font-size:0.82rem; }
  .builder-row input[type=range] { accent-color:#f59e0b; flex:1; min-width:60px; padding:0; }
  .builder-row .type-sel { width:68px; flex-shrink:0; }
  .builder-row .ing-sel  { width:120px; flex-shrink:0; }
  .builder-row .amt-inp  { width:52px; flex-shrink:0; text-align:right; }
  .builder-row .unit-lbl { font-size:0.72rem; color:#64748b; width:28px; flex-shrink:0; }
  .builder-row .del-btn  { background:none; border:none; color:#475569; cursor:pointer;
                           font-size:1rem; padding:0 4px; line-height:1; flex-shrink:0; }
  .builder-row .del-btn:hover { color:#ef4444; }
</style>
</head>
<body class="min-h-screen p-4">

<!-- Nav -->
<nav style="display:flex;gap:4px;background:#1e293b;border:1px solid #334155;border-radius:12px;padding:4px;margin-bottom:16px;">
  <a href="/"     style="flex:1;text-align:center;padding:8px 0;border-radius:8px;font-size:0.85rem;font-weight:600;text-decoration:none;color:#94a3b8;">⚙️ Control Panel</a>
  <a href="/shop" style="flex:1;text-align:center;padding:8px 0;border-radius:8px;font-size:0.85rem;font-weight:700;text-decoration:none;background:#f59e0b;color:#0f172a;">🍹 Drink Menu</a>
</nav>

<!-- Header -->
<div class="flex items-center justify-between mb-4">
  <div class="flex items-center gap-3">
    <span style="font-size:2rem">🍹</span>
    <div>
      <h1 style="font-size:1.25rem;font-weight:700;color:#f59e0b;margin:0">Drink Menu</h1>
      <p style="font-size:0.75rem;color:#64748b;margin:0">Debug mode — no payment</p>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:8px;font-size:0.85rem;">
    <span class="dot dot-red" id="status-dot"></span>
    <span id="status-text" style="color:#94a3b8">Checking…</span>
  </div>
</div>

<!-- Making banner -->
<div id="making-banner"><i class="fa fa-spinner fa-spin"></i> <span id="making-name">Making drink…</span></div>

<div style="display:grid;grid-template-columns:1fr;gap:16px;" id="main-grid">

  <!-- Slot overview -->
  <div class="card p-4">
    <p class="section-title mb-3">Loaded Slots</p>
    <div id="slot-overview" style="display:flex;flex-wrap:wrap;gap:6px;"></div>
  </div>

  <!-- Presets section -->
  <div class="card p-4">
    <p class="section-title mb-3">Preset Drinks</p>
    <div id="preset-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;">
      <span style="color:#475569;font-size:0.85rem">Loading…</span>
    </div>
  </div>

  <!-- Saved recipes section -->
  <div class="card p-4" id="saved-card" style="display:none">
    <p class="section-title mb-3">Saved Recipes</p>
    <div id="saved-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;"></div>
  </div>

  <!-- Builder section -->
  <div class="card p-4">
    <p class="section-title mb-3">Build a Drink</p>
    <div id="builder-rows" style="display:flex;flex-direction:column;gap:8px;"></div>
    <button onclick="addBuilderRow()"
      style="margin-top:10px;width:100%;padding:8px;background:#1e3a5f;border:1px dashed #334155;border-radius:8px;color:#64748b;font-size:0.82rem;cursor:pointer;transition:all 0.15s;"
      onmouseover="this.style.borderColor='#f59e0b';this.style.color='#f59e0b'"
      onmouseout="this.style.borderColor='#334155';this.style.color='#64748b'">
      + Add Ingredient
    </button>
    <div style="display:flex;gap:8px;margin-top:12px;padding-top:12px;border-top:1px solid #334155;">
      <input type="text" id="recipe-name" placeholder="Recipe name to save…"
        style="flex:1;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:6px;padding:0.4rem 0.6rem;font-size:0.85rem;">
      <button class="btn btn-slate" style="white-space:nowrap" onclick="saveRecipe(this)">
        <i class="fa fa-floppy-disk"></i> Save
      </button>
    </div>
    <button class="btn btn-amber" style="width:100%;margin-top:8px" onclick="makeCustom()">
      <i class="fa fa-play"></i> Make it
    </button>
  </div>

  <!-- Log section -->
  <div class="card p-4">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <p class="section-title">Serial Log</p>
      <button class="btn btn-slate" style="padding:2px 8px;font-size:0.75rem"
              onclick="document.getElementById('log').innerHTML=''">
        <i class="fa fa-trash"></i> Clear
      </button>
    </div>
    <div id="log"></div>
  </div>
</div>

<script>
const socket = io();
let connected = false;
let availableSpirits = {};  // slug → slot
let availableMixers  = {};  // slug → slot

const PRESETS = [
  { name:"Gin & Tonic",         emoji:"🍸", spirit:"gin",           spiritCl:4,  mixer:"tonic-water",  mixerMl:150 },
  { name:"Gin & Bitter Lemon",  emoji:"🍋", spirit:"gin",           spiritCl:4,  mixer:"bitter-lemon", mixerMl:150 },
  { name:"Rum & Cola",          emoji:"🥃", spirit:"rum",           spiritCl:4,  mixer:"cola",          mixerMl:150 },
  { name:"Vodka Cola",          emoji:"🖤", spirit:"vodka",         spiritCl:4,  mixer:"cola",          mixerMl:150 },
  { name:"Screwdriver",         emoji:"🍊", spirit:"vodka",         spiritCl:4,  mixer:"orangensaft",   mixerMl:150 },
  { name:"Tequila Sunrise",     emoji:"🌅", spirit:"tequila",       spiritCl:4,  mixer:"orangensaft",   mixerMl:120 },
  { name:"Jäger & Cola",        emoji:"🦌", spirit:"jaegermeister", spiritCl:4,  mixer:"cola",          mixerMl:150 },
  { name:"Trojka & Tonic",      emoji:"💚", spirit:"trojka-green",  spiritCl:4,  mixer:"tonic-water",   mixerMl:150 },
  { name:"Trojka & Bitter",     emoji:"❤️", spirit:"trojka-red",   spiritCl:4,  mixer:"bitter-lemon",  mixerMl:150 },
  { name:"Trojka & OJ",         emoji:"🟠", spirit:"trojka-red",   spiritCl:4,  mixer:"orangensaft",   mixerMl:150 },
  { name:"Tequila & Bitter",    emoji:"🌵", spirit:"tequila",       spiritCl:4,  mixer:"bitter-lemon",  mixerMl:150 },
  { name:"Rum & OJ",            emoji:"🌴", spirit:"rum",           spiritCl:4,  mixer:"orangensaft",   mixerMl:150 },
];

socket.on('connect', () => {
  socket.emit('get_status');
  loadIngredients();
});

socket.on('status', data => {
  connected = data.connected;
  document.getElementById('status-dot').className = 'dot ' + (connected ? 'dot-green' : 'dot-red');
  document.getElementById('status-text').textContent = connected
    ? `Connected · ${data.port || '/tmp/barbot.sock'}`
    : 'Serial not connected';
});

socket.on('disconnect', () => {
  connected = false;
  document.getElementById('status-dot').className = 'dot dot-red';
  document.getElementById('status-text').textContent = 'Socket disconnected — reload page';
});

socket.on('serial_out', data => {
  const el = document.getElementById('log');
  const line = document.createElement('div');
  const text = data.data;
  if      (text.startsWith('>'))               line.className = 'c-cmd';
  else if (text.startsWith('[shop]'))           line.className = 'c-info';
  else if (/error|warn|skip/i.test(text))      line.className = 'c-err';
  else                                          line.className = 'c-out';
  line.textContent = text;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;

  if (text.includes('Drink queued')) {
    setTimeout(() => {
      document.getElementById('making-banner').style.display = 'none';
    }, 3000);
  }
});

async function loadIngredients() {
  try {
    const data = await fetch('/api/ingredients').then(r => r.json());
    availableSpirits = data.spirits || {};
    availableMixers  = data.mixers  || {};
    renderSlotOverview(data.slot_mapping || {});
    buildPresets();
    // Init builder with one spirit row + one mixer row if list is empty
    if (!document.querySelectorAll('.builder-row').length) {
      addBuilderRow('spirit');
      addBuilderRow('mixer');
    }
    loadSavedRecipes();
  } catch(e) {
    console.error('loadIngredients failed', e);
  }
}

function clLabel(cl) {
  return cl >= 10 ? `${cl}cl` : `${cl}cl`;
}

function buildPresets() {
  const grid = document.getElementById('preset-grid');
  grid.innerHTML = '';
  let shown = 0;
  for (const p of PRESETS) {
    const hasSpirit = p.spirit in availableSpirits;
    const hasMixer  = p.mixer  in availableMixers;
    const available = hasSpirit && hasMixer;
    const pours = Math.round(p.spiritCl / 2);  // 2cl per pour default
    const card = document.createElement('div');
    card.className = 'drink-card' + (available ? '' : ' unavailable');
    card.innerHTML = `
      <div class="emoji">${p.emoji}</div>
      <h3>${p.name}</h3>
      <div class="ingredients" style="display:flex;flex-wrap:wrap;gap:4px">
        <span class="pill spirit">${p.spirit} ×${pours}</span>
        <span class="pill mixer">${p.mixer} ${p.mixerMl}ml</span>
      </div>
      <button class="btn btn-amber" style="margin-top:auto"
        onclick="makeDrink(${JSON.stringify(p.name)}, ${JSON.stringify(p.spirit)}, ${pours}, ${JSON.stringify(p.mixer)}, ${p.mixerMl})">
        <i class="fa fa-play"></i> Make it
      </button>`;
    grid.appendChild(card);
    if (available) shown++;
  }
  if (shown === 0) {
    const msg = document.createElement('p');
    msg.style.cssText = 'color:#475569;font-size:0.85rem;grid-column:1/-1';
    msg.textContent = 'No preset drinks available — check slot configuration.';
    grid.appendChild(msg);
  }
}

// ── Slot overview ─────────────────────────────────────────────────────────────

function renderSlotOverview(slotMapping) {
  // Physical order along the rail: 1-4, then mixers A-D, then 5-8
  const ORDERED_SLOTS = [
    'Slot_1','Slot_2','Slot_3','Slot_4',
    'Slot_A','Slot_B','Slot_C','Slot_D',
    'Slot_5','Slot_6','Slot_7','Slot_8',
  ];
  const MIXER_SET = new Set(['Slot_A','Slot_B','Slot_C','Slot_D']);
  const el = document.getElementById('slot-overview');

  function row(slot, ingredient) {
    const isMixer = MIXER_SET.has(slot);
    const label   = slot.replace('Slot_', '');
    const empty   = !ingredient;
    const badgeColor = empty ? '#334155' : (isMixer ? '#38bdf8' : '#f59e0b');
    const text    = empty ? '—' : ingredient;
    const opacity = empty ? '0.45' : '1';
    return `<div style="display:flex;align-items:center;gap:4px;opacity:${opacity};
                        background:#1e293b;border-radius:6px;padding:3px 8px 3px 4px;white-space:nowrap">
      <span style="background:${badgeColor};color:#0f172a;border-radius:4px;padding:1px 5px;
                   font-size:0.68rem;font-weight:700;font-family:monospace">${label}</span>
      <span style="font-size:0.78rem;color:${empty?'#475569':'#cbd5e1'}">${text}</span>
    </div>`;
  }

  el.innerHTML = ORDERED_SLOTS.map(s => row(s, slotMapping[s])).join('');
}

// ── Builder ──────────────────────────────────────────────────────────────────

function spiritOptions(selected) {
  const opts = Object.keys(availableSpirits);
  return opts.length
    ? opts.map(s => `<option value="${s}"${s===selected?' selected':''}>${s}</option>`).join('')
    : '<option value="">— none —</option>';
}
function mixerOptions(selected) {
  const opts = Object.keys(availableMixers);
  return opts.length
    ? opts.map(m => `<option value="${m}"${m===selected?' selected':''}>${m}</option>`).join('')
    : '<option value="">— none —</option>';
}

function addBuilderRow(type='spirit', slug='', amount=null) {
  const row = document.createElement('div');
  row.className = 'builder-row';
  const isSpirit  = type === 'spirit';
  const defAmt    = (amount !== null && amount !== undefined) ? amount : (isSpirit ? 2 : 150);
  const max       = isSpirit ? 10 : 500;
  const step      = isSpirit ? 1  : 10;
  row.innerHTML = `
    <select class="type-sel" onchange="rowTypeChanged(this)">
      <option value="spirit"${isSpirit?' selected':''}>Spirit</option>
      <option value="mixer"${!isSpirit?' selected':''}>Mixer</option>
    </select>
    <select class="ing-sel">${isSpirit ? spiritOptions(slug) : mixerOptions(slug)}</select>
    <input type="range" class="amt-slider" min="0" max="${max}" step="${step}" value="${defAmt}"
      oninput="this.nextElementSibling.value=this.value">
    <input type="number" class="amt-inp" min="0" max="${max}" step="${step}" value="${defAmt}"
      oninput="this.previousElementSibling.value=this.value">
    <span class="unit-lbl">${isSpirit?'pours':'ml'}</span>
    <button class="del-btn" onclick="this.closest('.builder-row').remove()" title="Remove">×</button>`;
  document.getElementById('builder-rows').appendChild(row);
}

function rowTypeChanged(sel) {
  const row      = sel.closest('.builder-row');
  const isSpirit = sel.value === 'spirit';
  row.querySelector('.ing-sel').innerHTML = isSpirit ? spiritOptions('') : mixerOptions('');
  const max  = isSpirit ? 10  : 500;
  const step = isSpirit ? 1   : 10;
  const defAmt = isSpirit ? 2 : 150;
  ['.amt-slider', '.amt-inp'].forEach(cls => {
    const el = row.querySelector(cls);
    el.max = max; el.step = step; el.value = defAmt;
  });
  row.querySelector('.unit-lbl').textContent = isSpirit ? 'pours' : 'ml';
}

function readBuilderRows() {
  const spirits = [], mixers = [];
  document.querySelectorAll('.builder-row').forEach(row => {
    const type   = row.querySelector('.type-sel').value;
    const slug   = row.querySelector('.ing-sel').value;
    const amount = parseFloat(row.querySelector('.amt-inp').value) || 0;
    if (!slug) return;
    if (type === 'spirit') spirits.push({ slug, pours: amount });
    else                   mixers.push({ slug, ml: amount });
  });
  return { spirits, mixers };
}

function makeCustom() {
  if (!connected) { logMsg('[shop] Serial not connected — open Control Panel to connect', 'c-err'); return; }
  const { spirits, mixers } = readBuilderRows();
  if (!spirits.length && !mixers.length) { logMsg('[shop] Add at least one ingredient', 'c-err'); return; }
  const name = document.getElementById('recipe-name').value.trim() || 'Custom';
  showBanner(name);
  socket.emit('make_drink_debug', { name, spirits, mixers });
}

function makeDrink(name, spirit, pours, mixer, ml) {
  if (!connected) { logMsg('[shop] Serial not connected — open Control Panel to connect', 'c-err'); return; }
  showBanner(name);
  socket.emit('make_drink_debug', {
    name,
    spirits: pours > 0 ? [{ slug: spirit, pours }] : [],
    mixers:  ml    > 0 ? [{ slug: mixer,  ml    }] : [],
  });
}

function makeSaved(recipe) {
  if (!connected) { logMsg('[shop] Serial not connected — open Control Panel to connect', 'c-err'); return; }
  showBanner(recipe.name);
  socket.emit('make_drink_debug', recipe);
}

function loadIntoBuilder(recipe) {
  document.getElementById('builder-rows').innerHTML = '';
  (recipe.spirits || []).forEach(s => addBuilderRow('spirit', s.slug, s.pours));
  (recipe.mixers  || []).forEach(m => addBuilderRow('mixer',  m.slug, m.ml));
  document.getElementById('recipe-name').value = recipe.name || '';
}

// ── Saved recipes ─────────────────────────────────────────────────────────────

async function loadSavedRecipes() {
  try {
    const data = await fetch('/api/saved_drinks').then(r => r.json());
    renderSaved(data.recipes || []);
  } catch(e) { console.error('loadSavedRecipes failed', e); }
}

function renderSaved(recipes) {
  const card = document.getElementById('saved-card');
  const grid = document.getElementById('saved-grid');
  if (!recipes.length) { card.style.display = 'none'; return; }
  card.style.display = '';
  grid.innerHTML = '';
  recipes.forEach(r => {
    const el = document.createElement('div');
    el.className = 'drink-card';
    const pills = [
      ...(r.spirits||[]).map(s => `<span class="pill spirit">${s.slug} ×${s.pours}</span>`),
      ...(r.mixers ||[]).map(m => `<span class="pill mixer">${m.slug} ${m.ml}ml</span>`),
    ].join('');
    el.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <h3>${r.name}</h3>
        <button onclick="deleteRecipe(${JSON.stringify(r.name)},this)"
          style="background:none;border:none;color:#475569;cursor:pointer;font-size:0.9rem;padding:0;"
          title="Delete">×</button>
      </div>
      <div class="ingredients" style="display:flex;flex-wrap:wrap;gap:4px">${pills}</div>
      <div style="display:flex;gap:6px;margin-top:auto;padding-top:8px">
        <button class="btn btn-slate" style="flex:1;font-size:0.78rem"
          onclick='loadIntoBuilder(${JSON.stringify(r)})'>
          <i class="fa fa-pen"></i> Edit
        </button>
        <button class="btn btn-amber" style="flex:1;font-size:0.78rem"
          onclick='makeSaved(${JSON.stringify(r)})'>
          <i class="fa fa-play"></i> Make
        </button>
      </div>`;
    grid.appendChild(el);
  });
}

async function saveRecipe(btn) {
  const name = document.getElementById('recipe-name').value.trim();
  if (!name) { logMsg('[shop] Enter a name before saving', 'c-err'); return; }
  const { spirits, mixers } = readBuilderRows();
  if (!spirits.length && !mixers.length) { logMsg('[shop] Nothing to save — add ingredients first', 'c-err'); return; }
  const orig = btn.className;
  try {
    await fetch('/api/saved_drinks', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ name, spirits, mixers }),
    });
    btn.className = orig.replace(/btn-\w+/, 'btn-green');
    setTimeout(() => { btn.className = orig; }, 1200);
    loadSavedRecipes();
  } catch(e) {
    btn.className = orig.replace(/btn-\w+/, 'btn-red');
    setTimeout(() => { btn.className = orig; }, 1200);
  }
}

async function deleteRecipe(name, btn) {
  btn.disabled = true;
  await fetch('/api/saved_drinks/' + encodeURIComponent(name), { method: 'DELETE' });
  loadSavedRecipes();
}

function showBanner(name) {
  document.getElementById('making-name').textContent = `Making: ${name}`;
  document.getElementById('making-banner').style.display = 'block';
}

function logMsg(text, cls) {
  const el   = document.getElementById('log');
  const line = document.createElement('div');
  line.className  = cls;
  line.textContent = text;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/shop")
def shop():
    return render_template_string(SHOP_HTML)


if __name__ == "__main__":
    print("BarBot Control Panel → http://0.0.0.0:7777")
    socketio.run(app, host="0.0.0.0", port=7777, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
