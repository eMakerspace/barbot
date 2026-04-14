#!/usr/bin/env python3
"""BarBot Web Control Panel — Flask + SocketIO (threading mode, no monkey-patch)."""

import threading
import time
import serial
import serial.tools.list_ports
from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "barbot-secret"
# threading mode: no monkey-patch, works reliably with pyserial
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Serial state ──────────────────────────────────────────────────────────────
ser: serial.Serial | None = None
ser_lock = threading.Lock()
reader_thread: threading.Thread | None = None


def list_ports():
    return [p.device for p in serial.tools.list_ports.comports()]


def serial_reader():
    """Background thread: read lines from serial and push to browser."""
    global ser
    while True:
        try:
            with ser_lock:
                if ser is None or not ser.is_open:
                    pass
                elif ser.in_waiting:
                    line = ser.readline().decode("utf-8", errors="replace").rstrip()
                    if line:
                        socketio.emit("serial_out", {"data": line})
                        continue
        except Exception as e:
            socketio.emit("serial_out", {"data": f"[reader error] {e}"})
        time.sleep(0.02)


# Start reader thread once at startup (it loops forever, checking ser)
_reader = threading.Thread(target=serial_reader, daemon=True)
_reader.start()


def send_gcode(cmd: str):
    global ser
    with ser_lock:
        if ser is None or not ser.is_open:
            socketio.emit("serial_out", {"data": "[not connected]"})
            return False
        try:
            ser.write((cmd.strip() + "\n").encode())
            ser.flush()
            socketio.emit("serial_out", {"data": f"> {cmd.strip()}"})
            return True
        except Exception as e:
            socketio.emit("serial_out", {"data": f"[send error] {e}"})
            return False


# ── SocketIO events ───────────────────────────────────────────────────────────

@socketio.on("list_ports")
def handle_list_ports():
    emit("ports", {"ports": list_ports()})


@socketio.on("connect_serial")
def handle_connect(data):
    global ser
    port = data.get("port", "")
    baud = int(data.get("baud", 115200))
    with ser_lock:
        try:
            if ser and ser.is_open:
                ser.close()
            ser = serial.Serial(port, baud, timeout=0.05)
        except Exception as e:
            emit("status", {"connected": False, "error": str(e)})
            emit("serial_out", {"data": f"[error opening {port}] {e}"})
            return
    emit("status", {"connected": True, "port": port, "baud": baud})
    emit("serial_out", {"data": f"[connected to {port} @ {baud}]"})


@socketio.on("disconnect_serial")
def handle_disconnect_serial():
    global ser
    with ser_lock:
        if ser and ser.is_open:
            ser.close()
    emit("status", {"connected": False})
    emit("serial_out", {"data": "[disconnected]"})


@socketio.on("send_gcode")
def handle_gcode(data):
    send_gcode(data.get("cmd", ""))


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
</style>
</head>
<body class="min-h-screen p-4">

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
<div class="card p-3 mb-4 flex flex-wrap gap-2 items-end">
  <div class="flex-1 min-w-36">
    <label class="section-title mb-1 block">Serial Port</label>
    <div class="flex gap-1">
      <select id="port-select" class="flex-1"></select>
      <button class="btn btn-slate btn-sm" onclick="refreshPorts()"><i class="fa fa-rotate"></i></button>
    </div>
  </div>
  <div class="w-28">
    <label class="section-title mb-1 block">Baud Rate</label>
    <select id="baud-select">
      <option value="115200" selected>115200</option>
      <option value="9600">9600</option>
      <option value="57600">57600</option>
      <option value="230400">230400</option>
    </select>
  </div>
  <button id="connect-btn" class="btn btn-green" onclick="toggleConnect()">
    <i class="fa fa-plug"></i> Connect
  </button>
</div>

<div class="grid grid-cols-1 lg:grid-cols-3 gap-4">

  <!-- LEFT: Safety + Stepper -->
  <div class="flex flex-col gap-4">

    <div class="card p-4 flex flex-col items-center gap-3">
      <p class="section-title">Safety</p>
      <button id="estop" onclick="gcode('M0.1',this)">
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
      <button class="btn btn-amber w-full" onclick="gcode('G28',this)">
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
      <div>
        <label class="text-xs text-slate-400 block mb-1">Absolute step (G0 X…)</label>
        <div class="flex gap-2">
          <input type="number" id="abs-pos" placeholder="0" class="flex-1">
          <button class="btn btn-blue btn-sm" onclick="sendAbsMove(this)">Go</button>
        </div>
      </div>
    </div>
  </div>

  <!-- CENTER: Servo + Pumps + Wait -->
  <div class="flex flex-col gap-4">

    <div class="card p-4 flex flex-col gap-3">
      <p class="section-title">Servo Motor</p>
      <div>
        <div class="flex justify-between items-center mb-1">
          <label class="text-xs text-slate-300">Angle (0 – 180°)</label>
          <span class="val-badge" id="servo-val">90°</span>
        </div>
        <input type="range" id="servo-slider" min="0" max="180" value="90" step="1" class="w-full"
          oninput="document.getElementById('servo-val').textContent=this.value+'°'"
          onchange="gcode('G1 Z'+this.value)">
      </div>
      <button class="btn btn-blue w-full btn-sm"
        onclick="gcode('G1 Z'+document.getElementById('servo-slider').value,this)">
        <i class="fa fa-rotate"></i> Set Angle (G1)
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

socket.on('connect', () => refreshPorts());
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
    ? `Connected · ${data.port} @ ${data.baud}`
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
});

function refreshPorts() { socket.emit('list_ports'); }

function toggleConnect() {
  if (connected) {
    socket.emit('disconnect_serial');
  } else {
    const port = document.getElementById('port-select').value;
    const baud = document.getElementById('baud-select').value;
    if (!port) { alert('No serial port selected'); return; }
    socket.emit('connect_serial', { port, baud });
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
  const v = ((100 - document.getElementById('range-slider').value) / 100).toFixed(2);
  gcode(`G0.1 X${v}${buildAccelSuffix()}`, btn);
}
function sendAbsMove(btn) {
  const pos = document.getElementById('abs-pos').value;
  if (pos === '') return;
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


@app.route("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    print("BarBot Control Panel → http://0.0.0.0:7777")
    socketio.run(app, host="0.0.0.0", port=7777, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
