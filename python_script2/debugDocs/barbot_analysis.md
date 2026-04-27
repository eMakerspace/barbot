# Barbot V2 - Project Overview & Threading Analysis

## 1. Project Purpose

**Barbot** is a **Raspberry Pi-based automated bartender** that:
- Polls WooCommerce for drink orders
- Resolves recipes (spirits + mixers for each drink)
- Physically dispenses drinks using:
  - Stepper motor (X-axis positioning via HAT ESP32)
  - Servo optic (spirit pouring)
  - Peristaltic pumps (mixer dispensing via pump ESP32)
  - Load cell / HX711 scale (automatic mixer fill by weight)
  - Light curtain cup sensor (automated cup detection/removal)
  - Neopixel LEDs + 7-segment display (animations & feedback)

**User Interface:**
- 4x20 LCD display (via I2C PCF8574 backpack)
- Rotary encoder (quadrature + push button)
- Menu system with 12 modes (menu, run, error, mixing, teach, etc.)

---

## 2. Overall Architecture

### Core Modules

| Module | Responsibility |
|---|---|
| `main.py` | Startup orchestration, signal handling |
| `lcd_menu.py` | **MAIN THREAD**: 50 ms render loop, state machine, menu navigation |
| `hardware.py` | G-code serial driver (3 ESP32s), cup sensor, movement, dispensing |
| `orders.py` | Order processing, drink recipe execution, heartbeat thread |
| `encoder.py` | Quadrature HAL, 2 kHz polling thread for rotary input |
| `woo_client.py` | WooCommerce REST API wrapper |
| `mixer.py` | Recipe resolution (line items → drink specs) |
| `config.py` | JSON config management |
| `lcd.py` | Low-level I2C LCD write caching |
| `inventory.py` | Stock sync to WooCommerce |
| `progress.py` | Crash-safe order progress (disk-persisted JSON) |
| `store.py` | Product catalog caching |
| `logger.py` | Unified logging |

### Three ESP32 Microcontrollers (Serial G-code)

| ESP32 | Role | Commands | Serial Port |
|---|---|---|---|
| **HAT (barbotv2)** | Stepper (X), Servo, Cup sensor | G28, G0, G1, G5, T0, M0/M0.1 | `/dev/serial/by-id/usb-Espressif_USB_JTAG_...` |
| **Pump (esp32_pump)** | Pumps A–D, Scale HX711, Autonomous fill | G2, G2.1, G3, G3.1, G3.2, G3.3, G4 | `/dev/serial/by-id/usb-Silicon_Labs_CP2102...` |
| **Neopixel (LED ESP32)** | LED animations, 7-segment display | `-br`, `-done`, `-drinkready`, `-overduestrobe`, `-bl {n}` | `/dev/serial/by-id/usb-1a86_USB_Serial...` |

---

## 3. Threading Model

### All Threads in System

| Thread | Role | Spawn Location | Daemon | Lock Held | Risk |
|---|---|---|---|---|---|
| **Main (UI)** | 50 ms render loop | `main()` → `ui.run()` | No | `_lock` when reading UI state | Low |
| **Encoder HAL** | 2 kHz GPIO polling | `RotaryEncoder.start()` (from UI init) | Yes | None (calls callback outside lock) | **MEDIUM** |
| **Serial Reader (HAT)** | Read lines, enqueue, fire callbacks | `EspSerial.__init__()` (hardware init) | Yes | `_send_lock` only | **HIGH** |
| **Serial Reader (Pump)** | Read lines, enqueue, fire callbacks | `EspSerial.__init__()` (hardware init) | Yes | `_send_lock` only | **HIGH** |
| **Order Polling** | Fetch + process orders (blocking) | `_enter_run()` spawns `_poll()` | Yes | Minimal (reads `_polling_paused` without lock) | **MEDIUM** |
| **Heartbeat** | WooCommerce ping (15s interval) | `process_order()` | Yes | None | Low |
| **Work Task** | Homing, fetch, teach (short blocking) | `_begin_work()` | Yes | None | Low |
| **Auto-dismiss** | Dismiss 'working' mode after 2s | `_work_auto_dismiss()` | Yes | `_lock` | Low |
| **Update Thread** | Viscosity/config async updates | Various `_begin_work()` | Yes | `_lock` for state changes | Low |

### Thread Interaction Map

```
┌──────────────────────────────────────────────────────────────────┐
│                    MAIN THREAD (UI Render Loop)                  │
│  50 ms tick → _lock → dispatch mode handler → draw LCD           │
│  Rotation/Press → encoder callback → _lock → modify UI state     │
└─────────┬──────────────────────────┬──────────────────┬──────────┘
          │                          │                  │
    ┌─────▼──────┐           ┌──────▼────────┐    ┌───▼──────────┐
    │  Encoder    │           │  Order Polling│    │  Serial HAT  │
    │  2 kHz Poll │           │  Thread       │    │  & Pump      │
    │  + callback │           │               │    │  Readers     │
    │             │           │ fetch orders  │    │              │
    │  _on_rotate │           │ process_order │    │ emit events  │
    │  _on_press  │           │ → hw.make_drink    │ call cbs     │
    │             │           │               │    │ (NO LOCK!)   │
    └─────┬──────┘           └──────┬────────┘    └───┬──────────┘
          │                          │                  │
          │         _lock             │                │
          │         (brief)          │                │
          └─────────────────────┬───┴────────────────┘
                                │
                     ┌──────────▼──────────┐
                     │  UI State Variables  │
                     │ _mode, _polling_paused
                     │ _run_count, _cup_present
                     │ _retry_drink, etc    │
                     └─────────────────────┘
```

---

## 4. Critical Threading Issues & What Can Go Wrong

### 🔴 **ISSUE 1: Serial Reader Callbacks Run Without Lock (HIGH RISK)**

**Files:** `hardware.py:355-378` (callbacks from serial reader)

**The Problem:**
```python
# In EspSerial._reader() thread (daemon, HIGH priority):
m = _CUP_RE.search(line)
if m and self._on_cup_state:
    present = m.group(1) == "PRESENT"
    self._on_cup_state(present)  # ← Calls _on_cup_state_changed()

# Which does:
def _on_cup_state_changed(self, present: bool):
    self._cup_present = present  # ← NO LOCK! Direct assignment
    self._cup_state_changed.set()  # ← Thread-safe, OK
```

**Race Condition Scenario:**
1. Serial reader thread (very high frequency) reads "[cup] PRESENT" from ESP32
2. At the **exact same moment**, main UI thread reads `_cup_present` under `_lock`
3. On CPython, boolean assignment **appears** atomic (GIL), but this is **NOT guaranteed** by the language
4. **Worst case:** Main thread reads stale `_cup_present=False` while serial thread just wrote `True`
5. **Impact:** `wait_for_cup()` continues thinking no cup present; next pour starts without cup detection

**Mitigation Currently:**
- CPython's GIL makes this *unlikely* in practice
- `threading.Event.set()` is thread-safe (uses locks internally)
- But no formal language guarantee exists

**What Could Go Wrong:**
- ✅ Fast toggle (cup in/out rapidly) → stale read
- ✅ Multiple serial readers competing to write `_cup_present`
- ❌ Very unlikely on Pi with GIL, but fragile design

---

### 🔴 **ISSUE 2: Unbounded Queue in Serial Reader (HIGH RISK)**

**File:** `hardware.py:144-162` (EspSerial._reader)

**The Problem:**
```python
# In serial reader thread:
while self._running:
    chunk = self._ser.read(128)
    lines = buf.replace(b"\r\n", b"\n").split(b"\n")
    for raw in lines[:-1]:
        line = raw.decode(...).strip()
        if line:
            self._lines.put(line)  # ← UNBOUNDED QUEUE!
```

The queue `self._lines = queue.Queue()` has **no maxsize**. If serial reader enqueues faster than consumers dequeue:

**Memory Leak Scenario:**
1. Serial port stuck or returning garbage bytes
2. Reader thread enqueues millions of lines → memory grows
3. Eventually: **Out of Memory → Process killed**

**Likelihood:** Very low (serial is slow, consumers block)  
**Severity:** Process crash  
**Fix:** Add `maxsize=1000` to `queue.Queue()`

---

### 🟠 **ISSUE 3: Polling Loop Stuck on Unhandled Exception (MEDIUM RISK)**

**File:** `lcd_menu.py:1600-1607` (_poll daemon)

**The Problem:**
```python
def _poll():
    while not stop.is_set():
        try:
            pending = self.woo.fetch_all('orders', {'status': 'processing'})
            for order in sorted(pending, key=lambda o: o['id']):
                try:
                    self.orders.process_order(order)
                except Exception as e:
                    self.pause_polling(str(e)[:20] or f"Order {order.get('id')} Failed")
                    break  # ← Exits polling loop
        except Exception:  # ← Catches fetch_all() exception
            pass  # ← Silently swallows!
        stop.wait(self.config.poll_interval)
```

**Stuck Scenario:**
1. WooCommerce API returns 500 error → `fetch_all()` raises exception
2. Outer `except Exception: pass` catches & silently ignores
3. Polling loop continues, calls `stop.wait(poll_interval)` → sleeps 5 seconds
4. User has **no idea** polling failed
5. **If running headless:** Machine stuck, orders not being fetched, customers angry

**What Could Happen:**
- Network blip → silent retry (OK, recovers)
- Persistent API issue → polling dead, no error indication
- User thinks orders are being processed; they're not

**Current Mitigation:**
- Errors are logged (user can check logs, but won't see on LCD)
- Polling resumes after 5s if the error was transient
- **Missing:** Auto-escalation (e.g., show error on LCD after 5 retries)

---

### 🟠 **ISSUE 4: `_sync()` Timeout Silently Continues (MEDIUM RISK)**

**File:** `hardware.py:534-548` (synchronization barrier)

**The Problem:**
```python
def _sync(self, timeout: float = 30.0):
    if not self._esp:
        return
    self._esp.send(f"G0 X{self.x_position}")
    try:
        self._esp.wait_for(["Move done"], timeout=timeout)
    except TimeoutError:
        print("[HW] WARNING: sync barrier timed out – assuming move complete")
        # ← CONTINUES ANYWAY!

def move_x(self, position: int):
    # ...
    self._queue_move(position)
    try:
        self._esp.wait_for(["Move done"], timeout=30.0)
    except TimeoutError:
        print("[HW] WARNING: move timed out")
        # ← CONTINUES ANYWAY!
```

**Collision Scenario:**
1. `dispense_spirit()` calls `move_x(slot_position)`
2. Carriage starts moving (mechanical jam? slow stepper?)
3. After 30s, `wait_for("Move done")` times out
4. Function **continues anyway** without checking carriage position
5. Servo opens and pours **while carriage is mid-transit**
6. **Result:** Spirit pours into wrong cup, servo crashes into carriage

**Worst Case:**
- Carriage at position 2000 (halfway across)
- Timeout at 30s
- Function returns, servo opens → spirit pours over the gap between cups
- Next `move_x(5000)` collision with servo arm

**What Could Go Wrong:**
- ✅ Stepper current limit triggered (slow move)
- ✅ Ball screw jam
- ✅ Serial reader thread deadlocked → no "Move done" response
- ❌ Rare but **catastrophic if happens** (wasted alcohol, broken hardware)

---

### 🟠 **ISSUE 5: Polling Paused Flag Race (MEDIUM RISK)**

**File:** `lcd_menu.py:1583-1590` (_poll daemon)

**The Problem:**
```python
# In polling thread (daemon):
if paused:
    stop.wait(1.0)
    continue

# paused checked without _lock:
with self._lock:
    paused = self._polling_paused

# Meanwhile, in main thread (UI):
def pause_polling(self):
    with self._lock:
        self._polling_paused = True  # ← Sets flag
```

**Race Scenario:**
1. Polling thread checks `paused` → reads `False`
2. **Between checking and calling `process_order()`**, error occurs
3. Main thread (error handler) sets `_polling_paused = True`
4. Polling thread already committed to calling `process_order()`
5. **Off-by-one order:** One extra order processed before pause takes effect

**Consequence:**
- Extra drink made (already-failed order processed again)
- Order duplication risk (though `OrderProgress` prevents repeat on crash)
- **Minor:** Usually handled by retry/cancel UI

---

### 🟡 **ISSUE 6: Order Polling + Status Write Deduplication (MEDIUM RISK)**

**File:** `orders.py:59-112` + `woo_client.py:122-136`

**The Problem:**
```python
# In process_order():
try:
    # ... make all drinks ...
finally:
    self._stop_heartbeat(hb_thread)

# ... drinks complete, now:
self.woo.update_order_status(order_id, "completed")

# But if this fails (network):
# In woo_client.py:
def update_order_status(self, order_id, status, retries=3):
    for i in range(retries):
        try:
            # PUT /orders/{id} with new status
            return True
        except Exception:
            if i < retries - 1:
                time.sleep(2)
    return False  # ← Silently fails after 3 retries!
```

**Duplication Scenario:**
1. All drinks made successfully ✅
2. `update_order_status()` called to mark "completed"
3. Network blip → all 3 retries fail → function returns `False`
4. Order still shows "processing" in WooCommerce
5. **Next polling cycle (5s later):** Order re-fetched
6. **Entire order remade** (duplication!)

**Likelihood:** Network latency > 2s, slow WiFi  
**Impact:**  
- Extra drinks wasted
- Customer double-charged (if WooCommerce doesn't deduplicate)
- Alcohol inventory wrong

**Mitigation:**
- `OrderProgress` prevents drinking same drink twice in same order
- But doesn't prevent re-processing same order entirely

---

### 🟡 **ISSUE 7: Config File Write Not Atomic (MEDIUM RISK)**

**File:** `config.py` + `hardware.py:317-325` (calibration save)

**The Problem:**
```python
def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    # ← NO ATOMIC SWAP!
```

**Corruption Scenario:**
1. Main thread calls `config.save()` to write config
2. Writes are in progress (e.g., halfway through JSON)
3. **Power loss or SIGKILL** (Ctrl+C not graceful)
4. File is **partially written** → invalid JSON
5. **Next startup:** `json.load()` raises `JSONDecodeError`
6. **Application fails to start** (completely non-functional)

**Example:**
```json
{
  "slot_positions": {
    "Slot_1": 100,
    "Slot_2": 200,
    "Slot_3": 300
    // ← File truncated here, no closing braces
```

**Current Mitigation:**
- `load_json()` catches `FileNotFoundError` but not `JSONDecodeError`
- Backup mechanism: none
- **Fix:** Use `tempfile` + `os.replace()` for atomic writes

---

### 🟡 **ISSUE 8: LCD Write Cache Not Locked (MEDIUM RISK)**

**File:** `lcd.py:55-70` (write cache)

**The Problem:**
```python
class LcdDisplay:
    def _write_row(self, row, text):
        # ← No lock here
        if text == self._cache[row]:
            return  # ← Skip if unchanged
        self._cache[row] = text  # ← WRITE without lock!
        # ... send to I2C ...
```

Main thread holds `_lock` when calling `_write_row()`, but:
- Render loop: `with self._lock: self._write_row(...)`
- Other callers (e.g., `show_error()` from serial callback): **no lock!**

**Race Scenario:**
1. Serial reader calls `ui.show_error()` → tries to write LCD without `_lock`
2. Main render loop simultaneously updating `_cache[2]`
3. Collision → wrong text on LCD or I2C write fails
4. **Impact:** Garbled display, user confusion

**Severity:** Low (string slicing safe, worst case: flicker)

---

### 🟢 **ISSUE 9: Cup Removal Timeout Doesn't Raise (DESIGN, Not a Bug)**

**File:** `hardware.py:451-476` (wait_for_cup_removal)

**Behavior:**
```python
def wait_for_cup_removal(self):
    while True:
        self._cup_state_changed.wait(timeout=0.5)
        self._cup_state_changed.clear()
        if not self._cup_present:
            log_info("HWINT", "Cup removed!")
            return  # ← Success!
    
    # ← NO TIMEOUT EXCEPTION!
    # Loop runs indefinitely until cup is removed
```

**Intentional Design:**
- For unattended operation (nightclub events)
- If customer forgets their drink, system doesn't hang
- LED animations eventually stop, ready for next order
- **Trade-off:** Order completes even if customer never takes drink

**Risk:**
- ✅ System continues, doesn't block
- ⚠️ Next order may start with stale cup state (`_cup_present=True` from previous drink)
- 🔴 **Depends on:** Cup sensor reliably transitioning ABSENT → PRESENT

---

## 5. Data Flow: Order Processing Under Concurrency

### Happy Path (No Errors)

```
TIME    POLLING THREAD              ORDER PROCESSOR            MAIN THREAD (UI)
────────────────────────────────────────────────────────────────────────────────
0s      _poll() fetches orders
        fetch_all('processing')
        ─────────────────────► WooCommerce API
        ◄─────────────────────
        Got order #123
        
        calls process_order()
                               ────────────────────────────►
                               resolve(line_items)
                               
                               start_heartbeat(15s)
                               ◄────────────────────────────
                               (bg thread sends pings)
                               
        ┌───────────────────────────────────────────────────┐
        │ For each drink:                                  │
        │  - show_mixing(drink_num, total, name)           │
        │                          ─────────────────────►  (LCD update)
        │  - hw.make_drink(spec)                           │
        │    ├─ wait_for_cup()                            │
        │    │  └─ serial reader fires _on_cup_state()    │
        │    │     (sets _cup_present, signals event)      │
        │    ├─ move_x(slot_pos) + servo pour + settle    │
        │    ├─ move_x(mixer_slot) + tare + auto-fill     │
        │    ├─ move_to_idle()                            │
        │    └─ wait_for_cup_removal() [blocks ~2s or ∞]  │
        │  - progress.drink_done() [write to disk]         │
        │                                                  │
30s     [Last drink done]                                   │
        
        clear_mixing()
                               ─────────────────────────►  (LCD clear)
        
        stop_heartbeat()
        ◄────────────────────
        
        update_order_status(#123, "completed")
        ─────────────────────► WooCommerce
        ◄─────────────────────
        
35s     Order #123 complete, ready for next order
```

### Error Path: Spirit Dispense Fails

```
TIME    POLLING THREAD         HARDWARE                SERIAL READER         MAIN THREAD (UI)
─────────────────────────────────────────────────────────────────────────────────────────────
0s      process_order()
        
        make_drink()
        ├─ wait_for_cup() ✓
        ├─ dispense_spirit(slot_2, pour_ms, settle_ms)
        │  ├─ move_x(3000)
        │  │  send "G0 X3000"
        │  │                                    ◄─────── receive & enqueue
        │  │  wait_for("Move done", timeout=30s)
        │  │                                    ─────►   read "[HWER] Servo collision!"
        │  │                                    ◄─────── callback _on_error_state()
        │  │                                            (calls ui.show_error() ASYNC!)
        │  │  wait_for returns timeout ⚠️
        │  └─ Continues anyway [WARNING logged]
        │
        ├─ _pour_sequence() [servo opens, alcohol flows, but carriage not ready!]
        │
        ├─ move_x(5000) [next mixer slot]
        │  HW: servo still at pour angle, carriage mid-transit
        │  ◄───────────────────────────────────────────── COLLISION!
        │
        ◄──────────────────────────────────────────────── HardwareError raised!
        
        process_order() catches exception:
        ├─ clear_mixing()
        │                                                ─────►  (LCD: clear mixing view)
        ├─ Raise (exception propagates)
        │
        POLLING THREAD:
        ├─ Exception caught in _poll()
        ├─ pause_polling(str(e))
        │                                                ─────►  (LCD: error mode)
        │
        └─ break  [Exits polling loop, waits for RETRY/CANCEL]
                                                          
        MAIN THREAD:
        ┌────────────────────────────────────────────────┐
        │ User sees ERROR mode with RETRY/CANCEL selector│
        │ Rotates encoder ─► _on_rotate() ─► toggle retry│
        │ Presses button ──► _on_press() ──► confirm     │
        │                                    ├─ RETRY:   │
        │                                    │ clear pause
        │                                    │ → resume polling
        │                                    │
        │                                    ├─ CANCEL:  │
        │                                    │ skip_current_drink()
        │                                    │ advance progress
        │                                    │ → resume polling
        │                                    └─ back to menu
        └────────────────────────────────────────────────┘
```

---

## 6. Locking Strategy & Coverage

### Where Locks Are Used

```
┌──────────────────────────────────────────────────────────────┐
│ MUTEX LOCKS IN SYSTEM                                        │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│ _lock (LCDMenu._lock)                                        │
│   Purpose: Protect UI state during 50ms render loop          │
│   Coverage: _mode, _run_count, _polling_paused, etc.         │
│   Acquired by: Main render loop, encoder callbacks           │
│   NOT acquired by: Serial reader callbacks (← RISK!)         │
│                                                               │
│ _send_lock (EspSerial._send_lock)                            │
│   Purpose: Serialize sends to serial port (avoid garble)     │
│   Coverage: HAT serial writes                                │
│   Acquired by: send() method                                 │
│                                                               │
│ _lock (NeopixelSerial._lock)                                 │
│   Purpose: Serialize sends to neopixel serial                │
│   Coverage: LED animation writes                             │
│   Acquired by: send() method                                 │
│                                                               │
│ ATOMICS (NO EXPLICIT LOCKS, rely on GIL/language guarantees) │
│   - _cup_present (boolean read/write)                        │
│   - _cup_state_changed (threading.Event → thread-safe)       │
│   - _polling_paused (boolean check)                          │
│   - _retry_drink (boolean toggle)                            │
│                                                               │
└──────────────────────────────────────────────────────────────┘
```

### Lock Acquisition Pattern (Render Loop)

```python
def run(self):
    while self._alive:
        with self._lock:  # ← ACQUIRE
            self._dirty = ...
            if self._mode == 'menu':
                self._draw_menu()
            elif self._mode == 'run':
                self._draw_run()
            # ... etc
        # ← RELEASE _lock
        self._write_to_lcd()  # No lock (OK, LCD write is atomic)
        time.sleep(0.05)  # 50 ms loop
```

**Lock held for:** ~5-20ms (just enough to read UI state)  
**Not held during:** I2C LCD write (safe)  
**Critical:** Encoder callbacks MUST acquire lock before modifying state

---

## 7. Failure Modes Summary

| Issue | Severity | Likelihood | Impact | Type |
|---|---|---|---|---|
| Unprotected `_cup_present` boolean | 🟠 MEDIUM | Very Low | Missed cup detection | Race Condition |
| Unbounded serial queue | 🔴 HIGH | Very Low | Memory leak, process crash | Resource Leak |
| Polling stuck on exception | 🟠 MEDIUM | Medium | Orders not fetched, silent failure | Concurrency |
| Sync timeout continues | 🟠 MEDIUM | Low | Carriage/servo collision, spilled alcohol | Safety |
| Polling paused flag race | 🟡 LOW | Very Low | Extra drink made | Race Condition |
| Order deduplication missing | 🟠 MEDIUM | Medium | Drink made twice, duplication | Network |
| Config file write not atomic | 🟠 MEDIUM | Low | Startup failure, corrupted config | Crash Safety |
| LCD cache not locked | 🟡 LOW | Very Low | Display flicker | Race Condition |
| Cup removal blocks forever | 🟢 DESIGN | N/A | Unattended operation OK | Design Choice |

---

## 8. Recommendations for Improvement

### High Priority (Safety & Robustness)

1. **Fix serial reader queue overflow:**
   ```python
   self._lines = queue.Queue(maxsize=1000)
   ```

2. **Protect `_cup_present` with explicit lock:**
   ```python
   def _on_cup_state_changed(self, present: bool):
       with self._cup_lock:  # ← NEW
           self._cup_present = present
       self._cup_state_changed.set()
   ```

3. **Raise exception on `_sync()` timeout (don't continue):**
   ```python
   except TimeoutError:
       raise HardwareError(f"Carriage move timed out after {timeout}s — aborting")
   ```

4. **Atomic config writes:**
   ```python
   with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
       json.dump(data, tmp)
       tmp_path = tmp.name
   os.replace(tmp_path, path)  # ← Atomic on POSIX
   ```

### Medium Priority (Reliability)

5. **Add order deduplication:**
   ```python
   _processed_orders = set()  # Track (id, timestamp) pairs
   if order_id in _processed_orders:
       continue  # Skip already-processed
   ```

6. **Handle polling exception escalation:**
   ```python
   error_count = 0
   while True:
       try:
           fetch_all(...)
           error_count = 0
       except Exception as e:
           error_count += 1
           if error_count > 5:
               ui.show_network_error()
   ```

7. **Lock LCD write cache:**
   ```python
   def _write_row(self, row, text):
       with self._lcd_lock:  # ← NEW
           if text == self._cache[row]:
               return
   ```

### Low Priority (Code Quality)

8. **Add maxsize to UI state queue:**
   - Currently using direct attribute writes (OK)
   - Consider `queue.Queue` for safer cross-thread updates

---

## 9. Conclusion

**Barbot** is a **well-engineered embedded system** with **good error handling** and **crash safety** (OrderProgress prevents duplication). However, the **threading model has several fragile points:**

### What Works Well ✅
- Serial reader is robust (handles partial frames, ANSI codes)
- Order processing is idempotent (progress file prevents double-making)
- UI is responsive (50 ms render, lock held briefly)
- Heartbeat prevents WooCommerce re-polling
- Cup sensor state machine is resilient to bounces (quadrature encoder concept)

### What's Fragile ⚠️
- Serial reader callbacks run without locks (relies on CPython GIL)
- Timeout silently continues instead of failing (collision risk)
- No order deduplication (network delays can cause remakes)
- Config write not atomic (startup failure if power loss mid-write)

### Biggest Risk 🔴
**Carriage/servo collision due to `_sync()` timeout** — If stepper move hangs > 30s, function continues anyway. Servo opens while carriage is mid-transit → crash, broken hardware, wasted alcohol.

**Recommended mitigation:** Change `_sync()` timeout to raise `HardwareError` instead of logging and continuing.

