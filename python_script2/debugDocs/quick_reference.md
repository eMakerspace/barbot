# Barbot V2 - Quick Reference Guide

## What Is Barbot?

**An automated bartender** that:
1. Polls WooCommerce for drink orders
2. Parses recipes (spirits + mixers per drink)
3. Physically dispenses drinks:
   - **Stepper motor** (X-axis positioning, talks to HAT ESP32)
   - **Servo** (spirit bottle optic positioning, talks to HAT ESP32)
   - **Peristaltic pumps** (mixers, talks to pump ESP32)
   - **Scale/HX711** (auto-fill by weight, talks to pump ESP32)
   - **Cup sensor** (light curtain, detected by HAT ESP32)
   - **Neopixel LEDs** (animations, talks to neopixel ESP32)
4. Shows status on 4x20 LCD + rotary encoder UI

---

## Thread Organization (6 Active Threads)

| Thread | Role | Spawned | Lock | Risk |
|---|---|---|---|---|
| **Main (UI)** | 50 ms render loop | startup | `_lock` | ✅ Low |
| **Encoder** | 2 kHz GPIO polling | startup | `_lock` (callback) | 🟠 Med |
| **Serial HAT** | Read ESP32 cup/error/homing | startup | None! | 🔴 High |
| **Serial Pump** | Read ESP32 scale/fill events | startup | None! | 🔴 High |
| **Order Polling** | Fetch & process orders (blocking) | enter_run | Minimal | 🟠 Med |
| **Heartbeat** | Ping WooCommerce every 15s | process_order | None | ✅ Low |

### The Problem: Serial Readers Don't Use Locks

Serial reader threads (HAT & Pump ESP32) fire callbacks **WITHOUT acquiring `_lock`**:
```python
# In serial reader thread:
if "[cup] PRESENT" in line:
    self._on_cup_state(True)  # ← Called directly, NO LOCK!
        └─ self._cup_present = True  # Direct write to shared state!
```

While main thread reads under lock:
```python
# In main thread render loop:
with self._lock:
    if self._cup_present:  # ← Read under lock
        # ...
```

**Result:** Potential race condition (CPython GIL makes it *unlikely* but not guaranteed).

---

## Critical Issues & Risks

### 🔴 **CRITICAL: Carriage Collision (Timing Bug)**

**File:** `hardware.py:534-548` (_sync method)

**The Bug:**
```python
def _sync(self, timeout=30.0):
    self._esp.send(f"G0 X{self.x_position}")
    try:
        self._esp.wait_for(["Move done"], timeout=30)
    except TimeoutError:
        print("[HW] WARNING: timed out")
        # ← CONTINUES ANYWAY! This is the problem.
```

**Scenario:**
1. Carriage moves toward position 3000
2. Mechanical jam → move stalls
3. Timeout after 30s → function returns
4. **Servo opens and pours while carriage is still mid-transit**
5. Collision + spilled alcohol + broken hardware

**Fix:** Raise exception instead of continuing:
```python
except TimeoutError:
    raise HardwareError(f"Carriage move timed out after {timeout}s")
```

---

### 🟠 **HIGH: Cup Sensor Race Condition**

**File:** `hardware.py:355-378` (_on_cup_state_changed)

**The Bug:**
```python
# Serial reader thread (no lock):
def _on_cup_state_changed(self, present: bool):
    self._cup_present = present  # ← Unprotected write!

# Main thread (with lock):
with self._lock:
    if self._cup_present:  # ← Read protected, but write is not!
        # ...
```

**Why It Matters:**
- Cup detection is critical (triggers drink dispensing)
- Stale read = wrong cup state = dispensing without cup detected
- Race window very small but real

**Fix:**
```python
def _on_cup_state_changed(self, present: bool):
    with self._cup_lock:  # ← NEW
        self._cup_present = present
    self._cup_state_changed.set()
```

---

### 🟠 **MEDIUM: Polling Loop Silent Failure**

**File:** `lcd_menu.py:1600-1610` (_poll daemon)

**The Bug:**
```python
def _poll():
    while not stop.is_set():
        try:
            pending = self.woo.fetch_all(...)  # Could raise exception
        except Exception:
            pass  # ← Silently swallows errors!
        
        stop.wait(5)  # Sleep & retry
```

**Scenario:**
1. Network error in `fetch_all()` → exception
2. Caught & silently ignored
3. Poll loop sleeps 5 seconds
4. **User has no idea polling failed**
5. Orders not fetched, customers angry

**Fix:** Add error escalation:
```python
error_count = 0
while True:
    try:
        pending = fetch_all(...)
        error_count = 0
    except Exception as e:
        error_count += 1
        if error_count > 3:
            ui.show_network_error()
```

---

### 🟠 **MEDIUM: Order Duplication Risk**

**File:** `orders.py:111-112` + `woo_client.py:130`

**The Bug:**
```python
# After all drinks made:
self.woo.update_order_status(order_id, "completed")  # Could fail!

# If network error → order stays "processing"
# Next poll cycle (5s later) → order re-fetched
# → Entire order remade again
```

**Mitigation:**
- `OrderProgress` prevents drink duplication **within same order**
- But doesn't prevent re-processing same order entirely

**Fix:** Track processed order IDs:
```python
_processed_orders = set()
for order in pending:
    if order['id'] in _processed_orders:
        continue  # Skip already-processed
    _processed_orders.add(order['id'])
    process_order(order)
```

---

### 🟡 **MEDIUM: Unbounded Serial Queue**

**File:** `hardware.py:131` (queue.Queue initialization)

**The Bug:**
```python
self._lines = queue.Queue()  # ← No maxsize!
```

If serial reader gets stuck enqueuing (e.g., garbage data):
- Queue grows unbounded
- Eventually: **Out of Memory → Process killed**

**Fix:**
```python
self._lines = queue.Queue(maxsize=1000)
```

---

### 🟡 **MEDIUM: Config Write Not Atomic**

**File:** `config.py` + `hardware.py:322` (save_json)

**The Bug:**
```python
def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f)  # ← Direct overwrite, no atomicity
```

**Scenario:**
1. Writing config file
2. Power loss mid-write
3. File is corrupted (partial JSON)
4. **Next startup:** `json.load()` raises `JSONDecodeError`
5. **Application won't start**

**Fix:**
```python
import tempfile, os
with tempfile.NamedTemporaryFile(mode='w', delete=False, dir=path.parent) as tmp:
    json.dump(data, tmp)
    tmp_path = tmp.name
os.replace(tmp_path, path)  # ← Atomic on POSIX
```

---

## How the System Works (Happy Path)

```
TIME    ACTION
────────────────────────────────────────────────────────────────────
0s      Order polling daemon fetches "processing" orders from WooCommerce
        Found: Order #123 (2 drinks)
        
        Calls orders.process_order(#123)
        ├─ Spawns heartbeat thread (pings WooCommerce every 15s)
        │
        ├─ Drink 1: Margarita (2 oz tequila + 1 oz lime juice)
        │  ├─ UI shows "Mixing 1/2: Margarita"
        │  ├─ hardware.make_drink()
        │  │  ├─ wait_for_cup() [blocks until cup detected via serial]
        │  │  ├─ dispense_spirit(tequila_slot, 2oz)
        │  │  │  ├─ move_x(tequila_position)
        │  │  │  ├─ servo.open() + pour + servo.close()
        │  │  │  └─ settle delay
        │  │  ├─ dispense_mixer(lime_juice, 1oz)
        │  │  │  ├─ move_x(mixer_position)
        │  │  │  ├─ scale.tare()
        │  │  │  ├─ auto_fill(pump_n, 1oz) [fills until scale reads 1oz]
        │  │  │  └─ [FILL_END] received from ESP32
        │  │  ├─ move_to_idle()
        │  │  └─ wait_for_cup_removal() [blocks until cup removed or 5 min timeout]
        │  ├─ progress.drink_done() [write to disk: drink 1 complete]
        │
5s      ├─ Drink 2: ... [repeat]
        │
10s     ├─ Order complete, all drinks done
        ├─ woo.update_order_status(#123, "completed")
        │
        └─ Order polling resumes, fetches next order
```

---

## What Works Well ✅

- **Order processing is idempotent** (OrderProgress file prevents re-making same drink on crash)
- **Serial communication is robust** (handles partial frames, ANSI codes, etc.)
- **UI is responsive** (50 ms render loop, locks held briefly)
- **Heartbeat prevents WooCommerce re-polling** (15s ping during mixing)
- **Cup sensor debounced** (quadrature-like state machine for bounces)

---

## What Can Break ⚠️

1. **Carriage/servo collision** ← Most dangerous, can destroy hardware
2. **Cup sensor state race** ← Unlikely but possible (GIL-dependent)
3. **Silent polling failure** ← Network issues hidden from user
4. **Order duplication** ← Network latency can cause remakes
5. **Startup failure** ← Power loss during config write corrupts file

---

## Recommended Fixes (Priority Order)

### CRITICAL (Safety)
- [ ] Change `_sync()` timeout to raise exception (prevent collision)
- [ ] Add explicit lock to `_on_cup_state_changed()` (avoid race)

### HIGH (Reliability)
- [ ] Add maxsize to serial queue (prevent memory leak)
- [ ] Implement order deduplication (prevent remakes)
- [ ] Use atomic writes for config files

### MEDIUM (Robustness)
- [ ] Handle polling exception escalation (show error on LCD)
- [ ] Track processed order IDs across restarts

---

## Testing the Problems

### Test 1: Carriage Collision Risk
```python
# In hardware.py move_x(), change timeout to 5 seconds (instead of 30)
# Physically block the carriage
# Try to dispense → should see timeout & potential collision
# Fix: Should raise HardwareError instead
```

### Test 2: Cup Sensor Race
```python
# Rapid cup insert/remove while polling running
# Check if system ever misses cup detection
# Current: Very unlikely due to GIL
# Fixed: Would be guaranteed safe
```

### Test 3: Order Duplication
```python
# Introduce network delay in update_order_status() (e.g., sleep 30s)
# Let it timeout (3 retries * 2s = 6s, then give up)
# Next poll cycle will re-fetch & remake order
# Fix: Add order ID tracking
```

