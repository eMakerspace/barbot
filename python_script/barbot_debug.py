#!/usr/bin/env python3
"""BarBot Serial Debug Page — diagnose why GCode is not reaching the ESP32."""

import threading
import time
import serial
import serial.tools.list_ports
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

# ── Serial state ──────────────────────────────────────────────────────────────
ser: serial.Serial | None = None
ser_lock = threading.Lock()

# Shared log: list of dicts [{ts, level, msg}]
log: list[dict] = []
log_lock = threading.Lock()
LOG_MAX = 400


def ts() -> str:
    return time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"


def push_log(level: str, msg: str):
    entry = {"ts": ts(), "level": level, "msg": msg}
    with log_lock:
        log.append(entry)
        if len(log) > LOG_MAX:
            log.pop(0)


def serial_reader():
    """Background thread: read lines from serial and push to log."""
    global ser
    push_log("info", "Reader thread started")
    while True:
        try:
            with ser_lock:
                if ser is None or not ser.is_open:
                    pass
                else:
                    waiting = ser.in_waiting
                    if waiting:
                        raw = ser.readline()
                        line = raw.decode("utf-8", errors="replace").rstrip()
                        hex_repr = raw.hex(" ")
                        push_log("rx", f"RX ({len(raw)}B hex={hex_repr}): {line}")
        except Exception as e:
            push_log("error", f"Reader exception: {e}")
        time.sleep(0.02)


_reader = threading.Thread(target=serial_reader, daemon=True)
_reader.start()


# ── HTTP API ──────────────────────────────────────────────────────────────────

@app.route("/api/ports")
def api_ports():
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            "device": p.device,
            "description": p.description,
            "hwid": p.hwid,
        })
    push_log("info", f"Port scan: {[p['device'] for p in ports]}")
    return jsonify(ports=ports)


@app.route("/api/status")
def api_status():
    with ser_lock:
        if ser is None:
            info = {"connected": False, "reason": "ser is None"}
        elif not ser.is_open:
            info = {"connected": False, "reason": "ser.is_open == False",
                    "port": ser.port, "baud": ser.baudrate}
        else:
            try:
                waiting = ser.in_waiting
            except Exception as e:
                waiting = f"error: {e}"
            info = {
                "connected": True,
                "port": ser.port,
                "baud": ser.baudrate,
                "timeout": ser.timeout,
                "in_waiting": waiting,
                "writable": ser.writable(),
            }
    return jsonify(info)


@app.route("/api/connect", methods=["POST"])
def api_connect():
    global ser
    data = request.json or {}
    port = data.get("port", "")
    baud = int(data.get("baud", 115200))
    push_log("info", f"Connect attempt → port={port!r} baud={baud}")
    if not port:
        push_log("error", "Connect failed: no port specified")
        return jsonify(ok=False, error="No port specified")
    with ser_lock:
        try:
            if ser and ser.is_open:
                push_log("info", f"Closing existing connection on {ser.port}")
                ser.close()
            push_log("info", f"Opening {port} @ {baud} …")
            ser = serial.Serial(port, baud, timeout=0.1)
            push_log("info", f"Opened OK — is_open={ser.is_open} writable={ser.writable()}")
            return jsonify(ok=True, port=port, baud=baud)
        except Exception as e:
            push_log("error", f"Open failed: {e}")
            return jsonify(ok=False, error=str(e))


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    global ser
    with ser_lock:
        if ser and ser.is_open:
            push_log("info", f"Disconnecting {ser.port}")
            ser.close()
        else:
            push_log("info", "Disconnect called but already closed/None")
        ser = None
    return jsonify(ok=True)


@app.route("/api/send", methods=["POST"])
def api_send():
    global ser
    data = request.json or {}
    cmd = data.get("cmd", "").strip()
    push_log("info", f"Send request: cmd={cmd!r}")

    if not cmd:
        push_log("warn", "Send aborted: empty command")
        return jsonify(ok=False, error="Empty command")

    with ser_lock:
        if ser is None:
            push_log("error", "Send failed: ser is None (not connected)")
            return jsonify(ok=False, error="Not connected — ser is None")
        if not ser.is_open:
            push_log("error", f"Send failed: ser.is_open=False port={ser.port}")
            return jsonify(ok=False, error="Not connected — port closed")

        payload = (cmd + "\n").encode("utf-8")
        hex_repr = payload.hex(" ")
        push_log("info", f"Writing {len(payload)}B: hex={hex_repr}")
        try:
            n = ser.write(payload)
            ser.flush()
            push_log("tx", f"TX OK — wrote {n}B, flushed. cmd={cmd!r}")
            return jsonify(ok=True, bytes_written=n, cmd=cmd)
        except Exception as e:
            push_log("error", f"Write exception: {e}")
            return jsonify(ok=False, error=str(e))


@app.route("/api/log")
def api_log():
    since = int(request.args.get("since", 0))
    with log_lock:
        entries = log[since:]
        total = len(log)
    return jsonify(entries=entries, total=total)


@app.route("/api/log/clear", methods=["POST"])
def api_log_clear():
    with log_lock:
        log.clear()
    push_log("info", "Log cleared")
    return jsonify(ok=True)


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BarBot Serial Debugger</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #c9d1d9; font-family: system-ui, sans-serif; font-size: 14px; padding: 16px; }
h1 { color: #f0883e; margin-bottom: 4px; font-size: 1.3rem; }
.sub { color: #8b949e; font-size: 0.8rem; margin-bottom: 16px; }

.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }

.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
.card h2 { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.12em; color: #f0883e; margin-bottom: 10px; font-weight: 700; }

label { font-size: 0.78rem; color: #8b949e; display: block; margin-bottom: 3px; margin-top: 8px; }
label:first-child { margin-top: 0; }

input[type=text], input[type=number], select {
  background: #0d1117; border: 1px solid #30363d; color: #c9d1d9;
  border-radius: 5px; padding: 6px 8px; width: 100%; font-size: 0.85rem;
}
input:focus, select:focus { outline: 2px solid #f0883e; border-color: transparent; }

.row { display: flex; gap: 8px; align-items: flex-end; }
.row input, .row select { flex: 1; }

button {
  background: #21262d; border: 1px solid #30363d; color: #c9d1d9;
  border-radius: 6px; padding: 6px 14px; cursor: pointer; font-size: 0.82rem; font-weight: 600;
  transition: background 0.12s; white-space: nowrap;
}
button:hover { background: #30363d; }
button.primary { background: #1f6feb; border-color: #1f6feb; color: #fff; }
button.primary:hover { background: #388bfd; }
button.danger  { background: #da3633; border-color: #da3633; color: #fff; }
button.danger:hover  { background: #f85149; }
button.ok      { background: #238636; border-color: #238636; color: #fff; }
button.ok:hover      { background: #2ea043; }
button.warn    { background: #9e6a03; border-color: #9e6a03; color: #fff; }
button.warn:hover    { background: #d29922; }

#status-badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 20px; font-size: 0.78rem; font-weight: 700;
  background: #21262d; border: 1px solid #30363d; margin-bottom: 10px;
}
.dot { width: 9px; height: 9px; border-radius: 50%; background: #da3633; flex-shrink: 0; }
.dot.green { background: #2ea043; box-shadow: 0 0 6px #2ea04388; animation: pulse 1.6s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.5} }

#status-detail {
  font-family: monospace; font-size: 0.78rem; background: #0d1117;
  border: 1px solid #30363d; border-radius: 5px; padding: 8px;
  white-space: pre-wrap; min-height: 60px; color: #79c0ff;
}

/* Log panel */
#log {
  font-family: 'Courier New', monospace; font-size: 0.76rem;
  background: #0d1117; border: 1px solid #1f3a5f; border-radius: 6px;
  height: 400px; overflow-y: auto; padding: 8px;
}
.log-ts   { color: #484f58; user-select: none; }
.log-info { color: #79c0ff; }
.log-warn { color: #d29922; }
.log-error{ color: #f85149; }
.log-tx   { color: #3fb950; font-weight: bold; }
.log-rx   { color: #a371f7; }

.quick-btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.quick-btns button { font-size: 0.75rem; padding: 4px 10px; }

#result-box {
  font-family: monospace; font-size: 0.78rem; background: #0d1117;
  border: 1px solid #30363d; border-radius: 5px; padding: 8px;
  min-height: 36px; white-space: pre-wrap; margin-top: 8px; color: #c9d1d9;
}

.port-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; margin-top: 6px; }
.port-table th { text-align: left; color: #8b949e; padding: 3px 6px; border-bottom: 1px solid #30363d; }
.port-table td { padding: 3px 6px; border-bottom: 1px solid #21262d; font-family: monospace; }
</style>
</head>
<body>
<h1>🔧 BarBot Serial Debugger</h1>
<p class="sub">Diagnose why GCode is not reaching the ESP32 — every step is logged with timestamps and byte counts.</p>

<div class="grid">

  <!-- LEFT col -->
  <div style="display:flex;flex-direction:column;gap:12px;">

    <!-- Connection -->
    <div class="card">
      <h2>1 · Serial Connection</h2>
      <div id="status-badge"><span class="dot" id="status-dot"></span><span id="status-label">Unknown</span></div>

      <label>Port</label>
      <div class="row">
        <select id="port-select"><option value="">— scan first —</option></select>
        <button onclick="scanPorts()">Scan</button>
      </div>

      <label>Baud Rate</label>
      <select id="baud">
        <option value="115200" selected>115200</option>
        <option value="9600">9600</option>
        <option value="57600">57600</option>
        <option value="230400">230400</option>
      </select>

      <div class="row" style="margin-top:10px;">
        <button class="ok"     onclick="connect()">Connect</button>
        <button class="danger" onclick="disconnect()">Disconnect</button>
        <button             onclick="getStatus()">Refresh Status</button>
      </div>

      <label style="margin-top:10px;">Port Details</label>
      <pre id="status-detail">Click "Refresh Status"…</pre>
    </div>

    <!-- Port list -->
    <div class="card">
      <h2>2 · Available Ports</h2>
      <button onclick="scanPorts()">Scan Ports</button>
      <table class="port-table" id="port-table">
        <thead><tr><th>Device</th><th>Description</th><th>HW ID</th></tr></thead>
        <tbody><tr><td colspan="3" style="color:#484f58;">Not scanned yet</td></tr></tbody>
      </table>
    </div>

    <!-- Send GCode -->
    <div class="card">
      <h2>3 · Send GCode</h2>
      <label>Command</label>
      <div class="row">
        <input type="text" id="cmd-input" placeholder="e.g. G28" onkeydown="if(event.key==='Enter')sendCmd()">
        <button class="primary" onclick="sendCmd()">Send</button>
      </div>

      <div class="quick-btns">
        <button class="warn" onclick="sendQuick('G28')">G28 Home</button>
        <button class="warn" onclick="sendQuick('M0')">M0 Stop</button>
        <button class="warn" onclick="sendQuick('M1')">M1 Continue</button>
        <button class="warn" onclick="sendQuick('G3')">G3 Scale</button>
        <button class="warn" onclick="sendQuick('G0.1 X0.50')">G0.1 X0.50</button>
        <button class="warn" onclick="sendQuick('G1 Z90')">G1 Z90 Servo</button>
        <button class="warn" onclick="sendQuick('G2 I0 D500')">Pump0 500ms</button>
        <button class="warn" onclick="sendQuick('T0 D500')">T0 D500 Wait</button>
      </div>

      <div id="result-box">Result will appear here…</div>
    </div>

    <!-- Raw bytes -->
    <div class="card">
      <h2>4 · Send Raw Bytes (hex)</h2>
      <label>Hex string (space-separated, e.g. <code>47 32 38 0a</code> = "G28\n")</label>
      <div class="row">
        <input type="text" id="hex-input" placeholder="47 32 38 0a">
        <button class="warn" onclick="sendHex()">Send Hex</button>
      </div>
      <div class="quick-btns">
        <button onclick="document.getElementById('hex-input').value='47 32 38 0a'">G28↵</button>
        <button onclick="document.getElementById('hex-input').value='4d 30 0a'">M0↵</button>
        <button onclick="document.getElementById('hex-input').value='4d 31 0a'">M1↵</button>
        <button onclick="document.getElementById('hex-input').value='0a'">↵ (LF only)</button>
      </div>
    </div>

  </div>

  <!-- RIGHT col: log -->
  <div class="card" style="display:flex;flex-direction:column;gap:8px;">
    <h2>Live Debug Log</h2>
    <div class="row">
      <button onclick="clearLog()">Clear Log</button>
      <button onclick="togglePause()" id="pause-btn">Pause</button>
      <span style="font-size:0.75rem;color:#8b949e;" id="log-count">0 entries</span>
    </div>

    <div style="font-size:0.72rem;color:#8b949e;font-family:monospace;">
      <span style="color:#79c0ff;">■ INFO</span> &nbsp;
      <span style="color:#3fb950;">■ TX</span> &nbsp;
      <span style="color:#a371f7;">■ RX</span> &nbsp;
      <span style="color:#f85149;">■ ERROR</span> &nbsp;
      <span style="color:#d29922;">■ WARN</span>
    </div>

    <div id="log"></div>

    <div class="row">
      <label style="margin:0;color:#8b949e;">Auto-scroll</label>
      <input type="checkbox" id="autoscroll" checked style="width:auto;margin-left:4px;">
    </div>
  </div>

</div>

<script>
let logCursor = 0;
let paused = false;
let pollTimer = null;

// ── Log polling ───────────────────────────────────────────────────────────────
function startPoll() {
  pollTimer = setInterval(pollLog, 500);
}

async function pollLog() {
  if (paused) return;
  try {
    const r = await fetch(`/api/log?since=${logCursor}`);
    const data = await r.json();
    if (data.entries.length > 0) {
      logCursor = data.total;
      appendLogEntries(data.entries);
    }
    document.getElementById('log-count').textContent = `${data.total} entries`;
  } catch(e) {
    // server not reachable
  }
}

function appendLogEntries(entries) {
  const el = document.getElementById('log');
  for (const e of entries) {
    const div = document.createElement('div');
    const cls = { info:'log-info', warn:'log-warn', error:'log-error', tx:'log-tx', rx:'log-rx' }[e.level] || 'log-info';
    div.innerHTML = `<span class="log-ts">${e.ts}</span> <span class="${cls}">[${e.level.toUpperCase()}]</span> ${escHtml(e.msg)}`;
    el.appendChild(div);
  }
  if (document.getElementById('autoscroll').checked) {
    el.scrollTop = el.scrollHeight;
  }
  // cap DOM nodes
  while (el.children.length > 600) el.removeChild(el.firstChild);
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function togglePause() {
  paused = !paused;
  document.getElementById('pause-btn').textContent = paused ? 'Resume' : 'Pause';
}

async function clearLog() {
  await fetch('/api/log/clear', { method: 'POST' });
  document.getElementById('log').innerHTML = '';
  logCursor = 0;
}

// ── Connection ────────────────────────────────────────────────────────────────
async function scanPorts() {
  const r = await fetch('/api/ports');
  const data = await r.json();
  const sel = document.getElementById('port-select');
  sel.innerHTML = data.ports.length
    ? data.ports.map(p => `<option value="${p.device}">${p.device}</option>`).join('')
    : '<option value="">— No ports found —</option>';

  const tbody = document.querySelector('#port-table tbody');
  tbody.innerHTML = data.ports.length
    ? data.ports.map(p =>
        `<tr><td>${escHtml(p.device)}</td><td>${escHtml(p.description)}</td><td style="color:#484f58">${escHtml(p.hwid)}</td></tr>`
      ).join('')
    : '<tr><td colspan="3" style="color:#f85149;">No ports found</td></tr>';
}

async function connect() {
  const port = document.getElementById('port-select').value;
  const baud = document.getElementById('baud').value;
  const r = await fetch('/api/connect', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ port, baud: parseInt(baud) })
  });
  const data = await r.json();
  await getStatus();
}

async function disconnect() {
  await fetch('/api/disconnect', { method: 'POST' });
  await getStatus();
}

async function getStatus() {
  const r = await fetch('/api/status');
  const data = await r.json();
  const dot = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  if (data.connected) {
    dot.className = 'dot green';
    label.textContent = `Connected · ${data.port} @ ${data.baud}`;
  } else {
    dot.className = 'dot';
    label.textContent = `Disconnected · ${data.reason || ''}`;
  }
  document.getElementById('status-detail').textContent = JSON.stringify(data, null, 2);
}

// ── Send ──────────────────────────────────────────────────────────────────────
async function sendCmd() {
  const cmd = document.getElementById('cmd-input').value.trim();
  if (!cmd) return;
  const r = await fetch('/api/send', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ cmd })
  });
  const data = await r.json();
  const box = document.getElementById('result-box');
  box.style.color = data.ok ? '#3fb950' : '#f85149';
  box.textContent = JSON.stringify(data, null, 2);
}

function sendQuick(cmd) {
  document.getElementById('cmd-input').value = cmd;
  sendCmd();
}

async function sendHex() {
  const hexStr = document.getElementById('hex-input').value.trim();
  if (!hexStr) return;
  // decode hex to a string command and send via /api/send as raw
  // We'll just send the decoded text
  try {
    const bytes = hexStr.split(/\s+/).map(h => parseInt(h, 16));
    const decoded = String.fromCharCode(...bytes);
    const cmd = decoded.replace(/\n$/, ''); // strip trailing newline since /api/send adds it
    document.getElementById('cmd-input').value = cmd;
    await sendCmd();
  } catch(e) {
    document.getElementById('result-box').textContent = `Hex parse error: ${e}`;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('load', () => {
  scanPorts();
  getStatus();
  startPoll();
});
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    push_log("info", "BarBot Serial Debugger starting on port 7778")
    print("BarBot Serial Debugger → http://0.0.0.0:7778")
    app.run(host="0.0.0.0", port=7778, debug=False, use_reloader=False)
