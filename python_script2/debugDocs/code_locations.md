# Barbot V2 - Issue Locations & Line References

## Critical Issues - Quick Location Guide

### 🔴 Issue 1: Carriage/Servo Collision
**File:** `hardware.py:534-548`
```python
534 │ def _sync(self, timeout: float = 30.0):
    │     """Wait for HAT command queue to drain."""
    │     if not self._esp:
    │         return
    │     self._esp.send(f"G0 X{self.x_position}")
    │     try:
540 │         self._esp.wait_for(["Move done"], timeout=timeout)
    │     except TimeoutError:
542 │         print("[HW] WARNING: sync barrier timed out – assuming move complete")
    │         # ← BUG: Should raise exception, not continue!
```

**Also affected:** `hardware.py:549-562` (move_x method calls _sync)

---

### 🔴 Issue 2: Cup Sensor Race Condition
**File:** `hardware.py:355-360`
```python
355 │ def _on_cup_state_changed(self, present: bool):
    │     """Called from serial reader thread when ESP32 reports cup state change."""
357 │     self._cup_present = present  # ← NO LOCK! Unprotected write
358 │     self._cup_state_changed.set()
359 │     log_info("HWINT", f"Cup state: {'PRESENT' if present else 'ABSENT'}")
```

**Reader triggered from:** `hardware.py:189-194`
```python
189 │ m = _CUP_RE.search(line)
    │ if m and self._on_cup_state:
    │     try:
192 │         present = m.group(1) == "PRESENT"
193 │         self._on_cup_state(present)  # ← Callback runs in serial reader thread
```

---

### 🟠 Issue 3: Polling Loop Silent Failure
**File:** `lcd_menu.py:1600-1610`
```python
1600│ try:
1601│     pending = self.woo.fetch_all('orders', {'status': 'processing'})
1602│     for order in sorted(pending, key=lambda o: o['id']):
    │         if stop.is_set():
    │             break
    │         with self._lock:
    │             self._run_last_id = order.get('id')
    │         try:
1609│             self.orders.process_order(order)
    │             with self._lock:
    │                 self._run_count  += 1
    │         except Exception as e:
    │             log_error("POLL", f"Error processing order {order.get('id')}: {e}")
    │             self.pause_polling(str(e)[:20] or f"Order {order.get('id')} Failed")
    │             break
1607│ except Exception:  # ← Outer exception handler
1608│     pass  # ← SILENTLY SWALLOWS ERRORS!
```

---

### 🟠 Issue 4: Order Duplication Risk
**File:** `orders.py:59-112` (process_order) + `woo_client.py:122-136` (update_order_status)

**Step 1 - Order processing completes:** `orders.py:100-112`
```python
100 │ except Exception as e:
    │     log_error("ORDER", f"Order #{order_id}: Error making drink: {e}")
    │     if self.ui:
    │         self.ui.clear_mixing()
    │     raise
105 │ finally:
    │     self._stop_heartbeat(hb_thread)
    │
    │ if self.ui:
    │     self.ui.clear_mixing()
110 │ self.progress.clear()
111 │ self.woo.update_order_status(order_id, "completed")  # ← Could fail!
```

**Step 2 - Status update (can fail):** `woo_client.py:122-136`
```python
122 │ def update_order_status(self, order_id, status, retries=3):
123 │     """Update order status on WooCommerce (retries 3×)."""
    │     for i in range(retries):
    │         try:
    │             response = self.api.put(f"orders/{order_id}", {'status': status})
    │             return True
128 │         except Exception as e:
    │             if i < retries - 1:
    │                 time.sleep(2)
130 │     log_error("WOO", f"Failed to update order {order_id} status after {retries} retries")
131 │     return False  # ← Silently fails!
```

---

### 🟡 Issue 5: Unbounded Serial Queue
**File:** `hardware.py:131` (EspSerial.__init__)
```python
131 │ self._lines: queue.Queue[str] = queue.Queue()  # ← NO MAXSIZE!
```

**Enqueuing happens in:** `hardware.py:161`
```python
161 │ self._lines.put(line)
```

---

### 🟡 Issue 6: Config Write Not Atomic
**File:** `hardware.py:317-325` (_save_calibration_factor)
```python
317 │ def _save_calibration_factor(self, factor: float):
    │     """Write the counts-per-gram factor into hardware_config.json."""
319 │     try:
320 │         cfg = json.loads(_HW_CONFIG_PATH.read_text())
321 │         cfg["scale_calibration_factor"] = round(factor, 6)
322 │         _HW_CONFIG_PATH.write_text(json.dumps(cfg, indent=4))  # ← Direct overwrite!
```

**Similar pattern in other config saves:**
- `config.py` (if it has save methods)
- `progress.py` (OrderProgress disk writes)

---

### 🟠 Issue 7: Polling Paused Flag Race
**File:** `lcd_menu.py:1583-1590` (_poll daemon)
```python
1583 │ # Check if polling is paused
1584 │ with self._lock:
1585 │     paused = self._polling_paused  # ← Read protected
    │
1587 │ if paused:
1588 │     # Wait while paused, but keep checking for stop signal
1589 │     stop.wait(1.0)
    │     continue
```

**Meanwhile, main thread sets the flag:** `lcd_menu.py` (pause_polling method, search for it)
```python
# In error handler:
self._polling_paused = True  # ← Write without lock (after callback)
```

---

### 🟡 Issue 8: LCD Write Cache Not Locked
**File:** `lcd.py:55-70`
```python
55  │ def _write_row(self, row, text):
    │     """Write to LCD row with caching."""
    │     # No lock here!
    │     if text == self._cache[row]:
    │         return
    │     self._cache[row] = text  # ← Write without lock
```

**Called from main thread (protected):**
```python
# In lcd_menu.py render loop:
with self._lock:
    self._write_row(...)  # Caller has lock
```

**But also called from serial callback (unprotected!):**
```python
# In hardware.py _on_error_state callback:
if self.ui:
    try:
        self.ui.show_error(name, severity)  # ← No lock!
```

---

### 🟢 Issue 9: Cup Removal Timeout (Design)
**File:** `hardware.py:451-476` (wait_for_cup_removal)
```python
451 │ def wait_for_cup_removal(self):
452 │     """Wait for cup to be removed after drink is done."""
    │     log_info("HWINT", f"Drink done — waiting for cup removal...")
    │     # ...
468 │     while True:
469 │         self._cup_state_changed.wait(timeout=0.5)
470 │         self._cup_state_changed.clear()
471 │         if not self._cup_present:
472 │             log_info("HWINT", "Cup removed!")
473 │             # ...
476 │             return
    │
    │ # ← NO TIMEOUT! Loop runs forever until cup removed
```

---

### 🟡 Issue 10: Marquee Scroll Offset
**File:** `lcd_menu.py:349-374` (_draw_menu)
```python
349 │ def _draw_menu(self):
    │     # ... label changes ...
    │     label_width = len(label) - 4  # ← can become negative
    │     if label_width > 0:
    │         self._marq_offset = (self._marq_offset + 1) % label_width
    │     text = label[self._marq_offset : self._marq_offset + 20]  # ← potential issue
```

---

## Module Dependency Graph

```
main.py
├── config.py (load configs)
├── woo_client.py (REST API)
├── hardware.py ────────────────────────────┐
│   ├── serial (EspSerial)                  │
│   │   ├── Reader thread                   │ ← Callbacks run here!
│   │   └── Callbacks (no lock)             │
│   ├── NeopixelSerial                      │
│   └── GPIO setup                          │
├── orders.py                               │
│   ├── woo_client (heartbeat)              │
│   ├── hardware (make_drink)  ─────────────┘
│   └── progress (crash safety)
├── lcd_menu.py (UI) ──────────────┐
│   ├── encoder.py (GPIO polling)  │ ← Callbacks run here!
│   │   └── RotaryEncoder          │
│   ├── lcd.py (I2C)               │
│   ├── orders (process_order)     │
│   └── hardware (UI reference)    │
│       └── Injected for callbacks │
└── Other modules...
```

---

## Lock Acquisition Order

**CURRENT STATE:**

```
Main thread:
  _lock (50ms, ~5-20ms held)
    ├─ Read: _mode, _run_count, _cup_present
    ├─ Write: _dirty, _mode, _nav
    └─ Call draw methods (may access UI state)

Encoder thread:
  _lock (brief)
    ├─ Read: encoder state (internally)
    └─ Write: _mode, _run_count, _retry_drink

Serial reader threads:
  ❌ NO LOCK ACQUIRED
    ├─ Write: _cup_present (RACE!)
    └─ Call: ui.show_error() (which might try to write LCD)

Order polling thread:
  _lock (minimal, just to check pause flag)
    └─ Read: _polling_paused

NeopixelSerial:
  _lock (brief, just serialize serial writes)
    └─ Write: serial port
```

**RECOMMENDED CHANGE:**

Add `_cup_lock` for cup sensor state:
```python
def __init__(self):
    self._lock = threading.Lock()  # UI state (existing)
    self._cup_lock = threading.Lock()  # Cup sensor (NEW)
    self._cup_present = False
    self._cup_state_changed = threading.Event()
```

---

## Testing Checklist

- [ ] **Test carriage timeout:** Physically block stepper, attempt dispense
- [ ] **Test cup race:** Rapid cup insert/remove during polling
- [ ] **Test polling error:** Kill network, monitor LCD & logs
- [ ] **Test order duplication:** Inject network delay in status update
- [ ] **Test queue overflow:** Send garbage data to serial port (stress test)
- [ ] **Test config corruption:** Kill power during config write (test on VM)
- [ ] **Test pause flag race:** Enable polling, then trigger error & RETRY rapidly

