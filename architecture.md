# Barbot V2 System Analysis

## 1. Overall Architecture and Module Responsibilities

This is a Raspberry Pi-based automated bartender that integrates three microcontrollers via serial interfaces using G-code protocol.

### Hardware Architecture

| Controller | Role | Serial Port |
|---|---|---|
| **barbotv2 (HAT ESP32)** | Stepper motor (X-axis), servo (spirit optic), cup sensor | `/dev/serial/by-id/usb-Espressif_USB_JTAG_...` |
| **esp32_pump** | Peristaltic pumps (Slot_A–D), HX711 load cell, autonomous fill | `/dev/serial/by-id/usb-Silicon_Labs_CP2102...` |
| **Neopixel ESP32** | LED animations, 7-segment display | `/dev/serial/by-id/usb-1a86_USB_Serial...` |

### Module Organization

| Module | Responsibility |
|---|---|
| `config.py` | Configuration management (BarbotConfig, HardwareConfig, AttributesConfig, StoreConfig) |
| `hardware.py` | G-code serial driver (EspSerial, HardwareInterface) with dual ESP32 interfaces and IPC server |
| `lcd_menu.py` | Full-featured UI (4x20 LCD + rotary encoder) with state machine (13 modes) |
| `lcd.py` | Low-level I2C LCD wrapper (address 0x3F, PCF8574 backpack) with write-caching |
| `encoder.py` | Quadrature rotary encoder with 2 kHz polling and debounce (300 ms default) |
| `orders.py` | WooCommerce order polling and drink recipe processing |
| `mixer.py` | Recipe resolution (line_items → DrinkSpec with spirits + mixers) |
| `woo_client.py` | REST API wrapper with pagination and batch updates |
| `inventory.py` | Stock synchronization (push_all, sync_ingredient) |
| `store.py` | Product catalog caching and recipe derivation |
| `progress.py` | Crash-safe order progress tracking (disk-persisted JSON) |
| `console.py` | Legacy CLI (replaced by LCDMenu but still present) |
| `logger.py` | Unified logging with elapsed-time timestamps |
| `main.py` | Startup orchestration and signal handling |

---

## 2. Startup/Initialization Sequence

### main.py Flow (Lines 39–77)

```
1. init_missing_configs()
   → Creates default JSON files if missing

2. load_env()
   → Reads .env for WooCommerce credentials

3. Load all config objects
   → BarbotConfig (slot_mapping, poll_interval)
   → HardwareConfig (positions, timing, serial ports)
   → StoreConfig (cached products)
   → AttributesConfig (spirits/mixers/bottle_properties)

4. Initialize WooClient
   → REST API ready (timeout: 15s)

5. Initialize HardwareInterface
   → Open HAT serial port (115200 baud)
   → Open Pump serial port (115200 baud)
   → Open Neopixel serial port (115200 baud)
   → If any fails: serial_error = True, enter simulation mode
   → Start IPC server on /tmp/barbot.sock

6. Create InventoryManager
   → Stock tracking and WooCommerce sync

7. Create OrderProcessor
   → Order fetching and execution

8. Create LCDMenu (UI entry point)
   → Inject UI reference into OrderProcessor and HardwareInterface

9. Execute homing sequence (blocking)
   → Servo parked at close_angle (180°) BEFORE stepper homes
   → Safety-critical ordering

10. Background daemon thread
    → Fetches WooCommerce attributes and products
    → Errors swallowed; user can re-fetch from Setup menu

11. Enter main render loop (50 ms iterations)
```

---

## 3. Main Event Loop and State Machine

### LCDMenu States (13 Distinct Modes)

| Mode | Purpose | Transitions |
|---|---|---|
| `menu` | Navigate Setup/Run/Maintenance menus | Rotate to scroll, press to select |
| `run` | Order polling loop active | Backlight timeout, error → pause |
| `info` | Display-only info screen | Auto or button to return |
| `working` | Task execution (homing, fetch, teach) | Spinner until `_work_done` flag |
| `confirm` | Yes/No dialog | Rotate to toggle, press to confirm |
| `mixing` | Show drink number during dispensing | Auto-dismiss when `_cup_present == False` |
| `cup_removed` | Brief 2 s confirmation | Auto-dismiss |
| `add_cup` | Flashing "PUT CUP IN!" prompt | Auto-dismiss when cup detected |
| `error` | Error display with RETRY/CANCEL | Rotate to select, press to confirm |
| `visc_edit` | Adjust viscosity per ingredient | Rotate to adjust, press to save |
| `x_move` | Jog stepper to position | Rotate L/R, press to confirm |
| `teach` | Jog stepper + save slot position | Rotate L/R, press to save |
| `num_entry` | Generic integer input | Rotate to increment, press to confirm |
| `pin_lock` | PIN code entry (feature-gated) | Digit input with timeout |

### Render Loop (Lines 709–732)

```
Every 50 ms:
  1. Acquire _lock
  2. Dispatch to mode-specific draw method
  3. Release _lock
  4. Write changed rows to LCD (cache prevents flicker)
```

### Input Flow

**Encoder (RotaryEncoder, daemon thread at 2 kHz)**
- Quadrature state machine with transition table
- Accumulates at ±STEPS_PER_DETENT (default: 4)
- Calls `_on_rotate(direction)` when detent crossed
- Calls `_on_press()` when button pressed (debounce: 300 ms)
- All callbacks acquire `_lock` before modifying state

**Critical Threading Note**: Encoder callbacks run in HAL thread, not main thread. All state mutations must be atomic under `_lock`.

---

## 4. All Execution Paths (Normal, Error, Edge Cases)

### A. Order Processing Path

```python
process_order(order_dict):
    1. resolver.resolve(line_items) → DrinkSpec[] (spirits + mixers)
    2. OrderProgress.resume_from_disk() → skip completed drinks on crash
    3. Spawn heartbeat thread (15 s interval, WooCommerce keep-alive)
    
    For each drink:
        ui.show_mixing(drink_num, total, name)
        hw.make_drink(spec, retry_mixers_only=False)
        progress.drink_done() → write to disk
    
    On exception:
        ui.clear_mixing()
        woo.update_order_status(order_id, "on-hold") ← Prevents re-polling
        Raise
    
    On success:
        progress.drink_done() → delete file
        woo.update_order_status(order_id, "completed")
```

### B. Drink Dispensing Path

```python
make_drink(spec, retry_mixers_only=False):
    UNLESS retry_mixers_only:
        1. wait_for_cup() → blocks until cup detected via serial
        2. _cup_confirm_countdown() → 6 s with LED animations
        
        3. For each spirit in spec.spirits:
            dispense_spirit(slot, pours, viscosity):
                - move_to_idle() → servo at close_angle
                - move_x(position) → blocks until "Move done"
                - _pour_sequence(pours, pour_ms, settle_ms):
                    - Open servo → wait pour_ms → close → wait settle
                    - _sync(timeout=30) → ensures all commands complete
    
    4. For each mixer in spec.mixers:
        dispense_mixer(slot, ml):
            - move_x(position) → wait "Move done"
            - G4 I{pump} W{weight} → autonomous fill
            - Wait for [FILL_END] response
    
    5. move_to_idle() → safe position with servo closed
    
    6. wait_for_cup_removal(timeout=300s):
        - Phase 1 (0–60 s): Green animations + cycling digits
        - Phase 2 (60 s+): Red strobe + "Take your drink!" prompt
        - Auto-continues after 5 min (does NOT raise)
```

### C. Cup Detection & Removal

**wait_for_cup()** (Lines 525–568)
- Blocks on `_cup_state_changed.wait()` event
- ESP32 reports `[cup] PRESENT` / `[cup] ABSENT` via serial
- Timeout: 30 s → raises `HardwareError`
- On detection: clears "add_cup" UI, starts countdown

**wait_for_cup_removal()** (Lines 663–670)
- Waits for cup ABSENT with escalating LED warnings
- **Does NOT raise on timeout** ← Critical issue
- Returns normally after 5 min even if cup still present

### D. Error Handling Path

**ESP32 Error Detection** (Lines 266–278)
- Regex: `[ERROR_STATE] code=N name=... effect=... severity=...`
- Callback `_on_error_state()` called from serial reader thread
- Sends `-e {effect}` to neopixel
- Calls `ui.show_error(name, severity)` if UI available

**Polling Pause on Error** (Lines 563–577)
- Severity ≥ 2 → pauses polling, shows RETRY/CANCEL selector
- RETRY → `retry_mixers_only=True` on next order (skip spirits, only mixers)
- CANCEL → resumes normal polling

### E. Retry Path

```python
_enter_run() → _poll() daemon thread:
    if _retry_drink flag is set:
        - Fetch last order
        - Call process_order(order, retry_mixers_only=True)
        - Skips cup detection & spirit pouring
        - Only retries mixer (pump) dispensing
```

---

## 5. Threading Model and Concurrency Risks

### Thread Architecture

| Thread | Role | Daemon | Lock | Risk Level |
|---|---|---|---|---|
| Main | 50 ms render loop | No | `_lock` | Low |
| Encoder HAL | 2 kHz quadrature + button (RotaryEncoder) | Yes | `_lock` | Medium |
| Serial reader (HAT) | Read lines, enqueue, trigger callbacks | Yes | None | High |
| Serial reader (pump) | Read lines, enqueue, trigger callbacks | Yes | None | High |
| Order polling | Fetch + process_order() (blocking) | Yes | None | Medium |
| Heartbeat | WooCommerce ping (15 s) | Yes | None | Low |
| Working task | Homing, fetch, etc. (short blocking) | Yes | None | Low |
| IPC server | Accept `/tmp/barbot.sock` connections | Yes | None | Low |

### Concurrency Risks

#### CRITICAL: Unprotected Serial Callbacks

**Files**: hardware.py:384–392, 397–406, 453–460

Callbacks `_on_cup_state()`, `_on_error_state()`, `_on_calibrated()` are called from the serial reader thread **without acquiring `_lock`**. They modify:
- `_cup_present` (boolean)
- `_cup_state_changed.set()` (threading.Event)
- Trigger callback invocations

**Mitigation**: CPython guarantees atomic boolean assignment and Event.set() is thread-safe. However, this is not a formal language guarantee and is fragile.

**Risk**: If UI thread reads `_cup_present` while serial thread writes, possible stale read (unlikely but possible).

#### HIGH: Unbounded Queue in EspSerial

**File**: hardware.py:148 (wait_for method)

```python
if line not in self.queue.queue:
    self.queue.put(line)
```

Queue has no maximum depth. If serial reader enqueues faster than consumers dequeue, memory grows unbounded.

**Likelihood**: Very low (serial is slow, consumers block). **Severity**: Memory leak if triggered.

#### MEDIUM: _sync() Barrier Race

**Files**: hardware.py:775–797

`_sync()` sends `G0 X{current_pos}` as no-op to serialize the command queue. If another thread sends a command before `_sync()` finishes waiting, ordering is violated.

**Example**: Polling thread calls `dispense_spirit()`, which calls `move_x()` + `_sync()`. Meanwhile, emergency stop is triggered, which sends `M0.1` before `_sync()` completes.

**Mitigation**: `_sync()` is always called serially from dispense operations (not interleaved). Risk is low in practice.

#### MEDIUM: Polling Pause Flag Race

**File**: lcd_menu.py:1764–1769

`_polling_paused` and `_retry_drink` are checked without `_lock` in the polling loop:

```python
while not stop.wait(self.hw.poll_interval):
    if self._polling_paused:
        ...
```

**Severity**: Low (bool read is atomic, worst case off-by-one poll cycle).

#### LOW: LCD Write Cache Not Locked

**Files**: lcd.py:55–70

`_write_row()` reads `_cache` without lock. Render loop holds `_lock` but `_write_row()` itself is unprotected.

**Severity**: Medium (write cache is per-row, unlikely collision, worst case temporary flicker).

---

## 6. Hardware Interaction (GPIO, I2C, Serial)

### GPIO Setup

```python
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
```

### Encoder Pins (RotaryEncoder)

| Pin | GPIO | Function |
|---|---|---|
| CLK | BCM 27 | Quadrature A |
| DT | BCM 17 | Quadrature B |
| SW | BCM 22 | Button (pull-up configured) |

### I2C LCD

- Address: `0x3F` (PCF8574 backpack)
- Bus: 1 (default Raspberry Pi I2C)
- Display: 20×4 character (RPLCD library)

### Serial Interfaces (All 115200 baud, 0.1 s timeout)

1. **HAT Serial** (barbotv2 ESP32)
   - Device: `/dev/serial/by-id/usb-Espressif_USB_JTAG_...`
   - Commands: G-code motion, servo, cup detection

2. **Pump Serial** (esp32_pump)
   - Device: `/dev/serial/by-id/usb-Silicon_Labs_CP2102...`
   - Commands: Pump control, scale tare/calibration, autonomous fill

3. **Neopixel Serial** (LED ESP32)
   - Device: `/dev/serial/by-id/usb-1a86_USB_Serial...`
   - Commands: Fire-and-forget LED animations (no response reading)

---

## 7. G-code Command Reference

### Motion Commands

| Command | Effect | Response | Notes |
|---|---|---|---|
| `G28` | Home stepper (calibrate X=0) | `"Homing successful, end pos = N"` | Safety-critical: servo must be closed first |
| `G0 X{n}` | Move to absolute position (0–6000) | `"Move done"` | Blocking, waits for carriage arrival |
| `G0.1 X{f}` | Fractional move (0.0–1.0) | `"Move done"` | Useful for soft positioning |
| `G1 Z{angle}` | Servo to angle (0–180°) | *(none)* | Immediate; no wait for servo arrival |
| `G5 P{p4} Q{p5}` | Set forbidden zones | *(none)* | Prevents servo opening in collision areas |
| `T0 D{ms}` | Delay (blocking) | *(none)* | Used for pour timing |
| `M0` | Graceful stop | *(none)* | Finish current command, then stop |
| `M0.1` | Emergency stop | *(none)* | Stop immediately (unsafe) |
| `M1` | Resume after stop | *(none)* | |

### Pump & Scale Commands

| Command | Effect | Response | Notes |
|---|---|---|---|
| `G2 I{n} D{ms}` | Run pump n for ms (non-blocking) | *(none)* | Pump returns to idle after ms |
| `G2.1 I{n} D{ms}` | Run pump n for ms (blocking) | *(none)* | Waits for pump to complete |
| `G3` | Read scale weight | `"Weight: X.XXg (raw: ...)"` | Used for testing scale |
| `G3.1` | Tare scale (zero offset) | `"Scale tared (offset: ...)"` | Must be called before fill |
| `G3.2 W{g}` | Calibrate with known weight | `"Calibrated: X.XXX counts/gram"` | Saved to hardware_config.json |
| `G3.3 F{cpg}` | Restore calibration factor | *(none)* | Loaded from disk on startup |
| `G4 I{n} W{g}` | Autonomous fill pump n to g grams | `[FILL_END] reason=... dispensed=...g` | Non-blocking on HAT; ESP32 handles timeout |

### Response Patterns

- `[cup] PRESENT` / `[cup] ABSENT` — Cup state change
- `[ERROR_STATE] code=N name=... effect=... severity=...` — Hardware error
- `[FILL_END] reason=target_reached` — Successful fill
- `[FILL_END] reason=timeout` — Fill timeout (HardwareError)
- `[FILL_END] reason=low_weight` — Scale reading too low (HardwareError)

---

## 8. WooCommerce/Network Integration

### WooClient (woo_client.py)

**Initialization**:
- Endpoint: `WOOCOMMERCE_URL` from `.env`
- Auth: `WOOCOMMERCE_KEY`, `WOOCOMMERCE_SECRET`
- Timeout: 15 s
- Library: `woocommerce.API` (python-woocommerce)

**Methods**:

| Method | Endpoint | Behavior |
|---|---|---|
| `fetch_all(endpoint, params)` | GET with pagination | Auto-pagination (100 per page), silently breaks on error |
| `batch_update_products(updates)` | POST `/products/batch` | Updates multiple products |
| `batch_update_variations(product_id, updates)` | POST `/products/{id}/variations/batch` | Updates variants |
| `update_order_status(order_id, status, retries=3)` | PUT `/orders/{id}` | Retries 3× with 2 s delay, silently fails on final retry |
| `send_heartbeat()` | POST `/wp-json/barmachine/v1/ping` | Custom endpoint, errors logged but ignored |
| `update_term_viscosity(attr_id, term_id, viscosity)` | PUT `/products/attributes/{id}/terms/{id}` | Updates ingredient viscosity |

### Order Polling Loop (lcd_menu.py:1754–1809)

```python
_poll() daemon thread (in run mode):
    loop:
        1. woo.send_heartbeat()
        2. Check _polling_paused & _retry_drink flags
        3. If paused: sleep(1s) and continue
        4. If retry: Fetch last order, call process_order(retry_mixers_only=True)
        5. Otherwise: Fetch all 'processing' orders
        6. For each order (sorted by ID):
            try:
                process_order(order)
            except Exception:
                pause_polling()
                show_error()
        7. stop.wait(poll_interval) → sleep until next poll
```

**Poll Interval**: 5 s (from slots_config.json)

### Heartbeat Thread (orders.py:73–81)

```python
heartbeat_thread(every 15 s during order processing):
    while order_processing:
        woo.send_heartbeat()
        sleep(15s)
```

Purpose: Notify WooCommerce that barbot is still working on the order (prevents re-polling).

### Inventory Sync (inventory.py)

| Method | Effect |
|---|---|
| `push_all()` | Sync all products/variations: mark in/out of stock based on mounted ingredients |
| `sync_ingredient(ingredient, in_stock)` | Find all products using this ingredient, toggle stock status |

---

## 9. All Possible System States

### Application Level

| State | Condition | Actions |
|---|---|---|
| **Startup** | Loading configs, initializing hardware, homing | Spinner on LCD |
| **Menu** | Idle, user navigating | Rotate/press encoder |
| **Setup** | Configuring slots, fetching store, managing viscosity | Menu navigation |
| **Run** | Polling WooCommerce for orders | Backlight timeout active |
| **Error** | Hardware or WooCommerce error occurred | Show RETRY/CANCEL selector |
| **Mixing** | Dispensing drink | Show drink number, progress bar |
| **Shutdown** | Graceful Ctrl+C or quit | Signal handlers, cleanup |

### Hardware States

| Component | States |
|---|---|
| **X-axis Stepper** | Homing, idle at home (0), idle at slot (500–6000), moving between positions |
| **Servo** | Closed (180°), pouring (85°), forbidden zone (blocked) |
| **Cup Sensor** | ABSENT, PRESENT, unknown (pre-detection) |
| **Pumps A–D** | Idle, running timed (G2), autonomous fill (G4) |
| **Scale** | Unzeroed, zeroed (tared), reading weight |

### Order States

| State | Location | Behavior |
|---|---|---|
| **Polling** | WooCommerce | Fetching 'processing' orders |
| **Processing** | Local queue | Heartbeat running, making drinks |
| **Paused** | UI (RETRY/CANCEL) | User chooses action |
| **Completed** | WooCommerce | Status updated to "completed", cleared from queue |
| **On-Hold** | WooCommerce | Order error occurred, no re-poll (prevents loop) |

---

## 10. Error Sources and Failure Modes

### A. Hardware Failures

#### Serial Port Unavailable
- **File**: hardware.py:__init__
- **Detection**: Exception on `serial.Serial(port, 115200)`
- **Response**: Set `serial_error = True`, enter simulation mode (timed sleeps)
- **Impact**: UI shows warning banner (lines 592–635), no real hardware interaction

#### Cup Sensor Timeout
- **File**: hardware.py:562–568
- **Detection**: `wait_for_cup()` blocks > 30 s
- **Response**: Raise `HardwareError`
- **Caught by**: `process_order()` exception handler
- **Impact**: Order moved to "on-hold", LED shows RED_FLASH_FAST

#### Homing Failure
- **File**: hardware.py:687–694
- **Detection**: G28 response doesn't contain "Homing successful"
- **Response**: Raise `HardwareError`
- **Caught by**: `_wait_work()` in LCD menu
- **Impact**: UI shows error, user can retry homing

#### Move Timeout
- **File**: hardware.py:742–743
- **Detection**: `_sync()` times out waiting for "Move done"
- **Response**: Log warning, **continue anyway**
- **Impact**: If carriage hasn't finished moving, next pour starts in transit (dangerous!)

#### Servo Forbidden Zone Violation
- **File**: hardware.py:854–856
- **Detection**: `in_forbidden_servo_zone()` returns True
- **Response**: Print warning, **return without pouring**
- **Impact**: Drink served incomplete with NO LCD indication to user

#### Pump Fill Failure
- **File**: hardware.py:915–918
- **Detection**: `[FILL_END] reason != "target_reached"`
- **Response**: Raise `HardwareError`
- **Caught by**: `process_order()` exception handler
- **Impact**: Order moved to "on-hold", user can RETRY (retry_mixers_only=True)

#### Scale Not Detected
- **File**: hardware.py:934–937
- **Detection**: G3.1 response contains "HX711 not"
- **Response**: Raise `HardwareError`
- **Impact**: Tare operation fails, autonomous fill unavailable

#### Calibration Loss
- **File**: hardware.py:432–453
- **Restoration**: Factor persisted to hardware_config.json
- **Fallback**: On restart, G3.3 restores factor; if file missing, firmware default used

### B. Network Failures

#### WooCommerce Unreachable
- **File**: woo_client.py:fetch_all
- **Detection**: Exception during HTTP request
- **Response**: Log error, return empty list `[]`
- **Caught by**: `_poll()` in lcd_menu.py
- **Impact**: No orders fetched, poll loop sleeps and retries (no alerting to user)

#### Order Status Update Fails
- **File**: woo_client.py:122–136
- **Detection**: HTTP error after 3 retries (2 s delay each)
- **Response**: Log error, **silently give up**
- **Critical Issue**: Order remains "processing" in WooCommerce
- **Impact**: Order re-polled on next cycle, same drinks made twice (idempotency violation!)

#### Heartbeat Timeout
- **File**: orders.py:75–78
- **Detection**: Exception during heartbeat send
- **Response**: Log warning, **continue**
- **Impact**: Website may timeout waiting for order completion

### C. Configuration Errors

#### Missing hardware_config.json
- **File**: main.py:77–81
- **Detection**: FileNotFoundError from load_json
- **Response**: Raise, application exits during startup
- **Impact**: Barbot completely non-functional

#### Invalid Slot Position
- **File**: hardware.py:position_for_slot
- **Detection**: Slot not found in config
- **Response**: Return `None`
- **Caught by**: `dispense_spirit()` / `dispense_mixer()`
- **Impact**: HardwareError raised, order on-hold

#### Ingredient Not in Mounted Slots
- **File**: hardware.py:1047–1048
- **Detection**: Drink spec has `slot=None`
- **Response**: Print warning, **skip pouring**
- **Impact**: Incomplete drink served silently (no LCD indication)

### D. UI/Input Failures

#### Encoder I2C Failure
- **File**: lcd_menu.py:235–243
- **Detection**: Exception from RotaryEncoder.start()
- **Response**: Try to display error on LCD, then raise
- **Impact**: UI non-functional, user can't interact

#### LCD I2C Failure
- **File**: lcd.py (RPLCD calls)
- **Detection**: Exception from write_row()
- **Response**: Propagates to _render()
- **Impact**: Render loop crashes, UI updates stop

#### Button Debounce Mismatch
- **File**: encoder.py:66
- **Debounce**: 300 ms default
- **Issue**: Very fast button presses (< 300 ms) skipped
- **Impact**: UI may not respond to rapid input

### E. Data Corruption Risks

#### Order Progress File Corruption
- **File**: progress.py (OrderProgress)
- **Corruption Scenario**: File truncated mid-write due to crash
- **Behavior**: `resume_from_disk()` catches JSONDecodeError, returns 0 (all drinks re-made)
- **Mitigation**: None (no atomic write or backup)

#### Config File Write Not Atomic
- **File**: config.py:save_json
- **Mechanism**: Direct overwrite (no tempfile + rename)
- **Scenario**: Crash mid-write leaves file partially written
- **Mitigation**: load_json catches FileNotFoundError, but JSONDecodeError will crash

#### Slot Mapping Changed During Poll
- **File**: config.py (BarbotConfig.slot_mapping)
- **Scenario**: Menu saves new mapping to disk while polling loop reads it
- **Mitigation**: Dict reference swap is atomic on CPython (CPython-specific guarantee, not language guarantee)

---

## 11. Critical Bugs and Race Conditions

### 1. **CRITICAL: Cup Removal Timeout Doesn't Prevent Next Order**

**File**: hardware.py:663–670

**Code**:
```python
def wait_for_cup_removal(self, timeout=300):
    # ... escalating LED effects ...
    if not self._cup_removed_event.wait(timeout):
        logger.warning(f"Cup not removed after {timeout}s; continuing anyway")
        return
```

**Problem**:
- `wait_for_cup_removal()` times out after 5 min but returns normally
- `_cup_present` remains `True` (never set back to `False`)
- Next order's `wait_for_cup()` sees `_cup_present=True` already and returns immediately
- Next order skips cup detection and proceeds immediately
- **Result**: Next drink dispensed into same cup or onto nothing

**Scenario**:
1. Order 1 completed, cup removal prompt active (5 min countdown)
2. User takes cup before end of countdown (≤ 5 min)
3. `_cup_present` correctly becomes `False`
4. Order 1 completes
5. BUT if user doesn't take cup after 5 min, timeout fires
6. `wait_for_cup_removal()` returns, `_cup_present` still `True`
7. Order 2 fetched immediately (polling interval only 5 s)
8. `wait_for_cup()` checks `_cup_present` and returns immediately
9. Dispense into old cup with order 1 remnants

**Fix**: Either raise `HardwareError` on timeout, or force `_cup_present = False` after timeout.

---

### 2. **CRITICAL: Order Re-polling on Status Update Failure**

**File**: woo_client.py:122–136

**Code**:
```python
def update_order_status(self, order_id, status, retries=3):
    for attempt in range(retries):
        try:
            self.api.put(f"orders/{order_id}", {"status": status})
            return
        except Exception as e:
            logger.error(f"Failed to update order {order_id}: {e}")
            time.sleep(2)
    # No exception raised; silently fail
```

**Problem**:
- If the final "completed" status write fails, order remains "processing" in WooCommerce
- `_poll()` fetches it again on the next cycle (5 s later)
- `process_order()` called again with same order
- **Result**: Same drinks dispensed twice

**Scenario**:
1. Order 1 drinks dispensed successfully
2. `update_order_status(order_id, "completed")` called
3. Network transient; all 3 retries fail
4. Exception logged; function returns normally
5. Order 1 still marked "processing" on WooCommerce
6. Polling loop fetches "processing" orders
7. Order 1 found again
8. `process_order(order_1)` called again
9. Same drinks made duplicate
10. User calls support (duplicate order issue)

**Root Cause**: No idempotency check, no de-dup on re-fetch.

**Fix**: 
- Move failed orders to "on-hold" as fallback if status update fails 3×
- Or: implement order ID de-duplication in `_poll()` (track `last_order_ids`)

---

### 3. **MEDIUM: Servo Forbidden Zone Silently Skips Pour**

**File**: hardware.py:854–856

**Code**:
```python
if self.in_forbidden_servo_zone(position):
    logger.warning(f"Position {position} in forbidden zone; cannot pour spirit")
    return  # ← Returns without raising
```

**Problem**:
- Drink is served **incomplete** (missing a spirit)
- User receives wrong drink (e.g., missing rum in Cuba Libre)
- **No LCD indication** to user or operator
- Order marked "completed" anyway

**Scenario**:
1. Forbidden zones set to protect from servo hitting carriage frame
2. A spirit slot is too close to forbidden zone
3. User doesn't realize (not communicated)
4. Order placed for drink using that spirit
5. `dispense_spirit()` called, position in forbidden zone
6. Warning logged, function returns without pouring
7. Drink continues (next spirit, then mixers)
8. User receives drink without one spirit
9. No error, no retry option

**Fix**: Raise `HardwareError` instead of returning silently, or show LCD warning.

---

### 4. **MEDIUM: No Order De-duplication**

**File**: lcd_menu.py:1792–1794

**Code**:
```python
orders = woo.fetch_all("orders", {'status': 'processing'})
for order in orders:
    process_order(order)
```

**Problem**:
- If `fetch_all()` returns the same order twice (edge case), it will be processed twice
- WooCommerce pagination edge case: same order appears in pages 1 and 2
- Very slow status write + fast polling: order fetched before status changes to "completed"

**Scenario**:
1. Order 1 processed
2. `update_order_status(order_id, "completed")` called
3. Network slow; write takes > 5 s to propagate
4. Polling cycle continues while write in flight
5. `fetch_all("orders", status='processing')` still returns order 1 (not yet updated)
6. Order 1 processed again
7. Duplicate drinks made

**Fix**: Track `last_processed_order_ids` and skip if seen.

---

### 5. **MEDIUM: Unprotected `_cup_present` Read/Write Across Threads**

**File**: hardware.py:384–392 (_on_cup_state_changed callback)

**Code**:
```python
def _on_cup_state_changed(self, *args):
    self._cup_present = "PRESENT" in msg  # ← No lock
    self._cup_state_changed.set()
```

**Problem**:
- Serial reader thread writes `_cup_present` without lock
- Main thread (render loop) reads `_cup_present` under `_lock`
- Polling thread reads `_cup_present` from `wait_for_cup()`
- **Race**: Serial write and main read can occur simultaneously

**Severity**: Low in practice (CPython atomic assignment), but not formally guaranteed.

**Mitigation**: `threading.Event` (.set() and .wait()) are thread-safe primitives. Boolean assignments are atomic on CPython.

**Better Fix**: Acquire lock in callback, or use `Queue` for state updates.

---

### 6. **MEDIUM: `_sync()` Timeout Continues Silently**

**File**: hardware.py:742–743

**Code**:
```python
if not self._sync(timeout=30):
    logger.warning(f"_sync() timed out; assuming move complete")
    # Continue anyway ← dangerous if move still in progress
```

**Problem**:
- If carriage move times out (> 30 s), function continues
- If carriage hasn't actually finished moving, next command proceeds while carriage in transit
- **Result**: Pumps dispensing while carriage moving, servo potentially in collision zone

**Scenario**:
1. `dispense_spirit()` called
2. `move_x(position)` enqueued
3. Carriage moves slowly (mechanical jam?)
4. `_sync()` times out after 30 s
5. Function continues to _pour_sequence()
6. Servo opens, pumps run **while carriage still moving**
7. Collision or incorrect pour location

**Fix**: Raise `HardwareError` on `_sync()` timeout, don't continue.

---

### 7. **MEDIUM: Polling Loop Stuck on Exception**

**File**: lcd_menu.py:1801–1804

**Code**:
```python
try:
    self.processor.process_order(order)
except Exception as e:
    logger.error(f"Error processing order: {e}")
    self.pause_polling()
    break  # ← Exits polling loop
```

**Problem**:
- If `process_order()` raises, polling loop breaks
- No more orders processed until user presses button (RETRY/CANCEL)
- If UI not responsive or user not present, machine is stuck

**Scenario**:
1. Unknown exception in `process_order()` (not HardwareError)
2. Polling loop breaks
3. User needs to see error and press RETRY
4. If running "headless" (no display), machine stuck indefinitely

**Fix**: Add timeout to pause_polling, or auto-resume after timeout.

---

### 8. **LOW: Config File Write Not Atomic**

**File**: config.py:save_json

**Code**:
```python
def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    # ← No atomic swap
```

**Problem**:
- Direct file overwrite (no tempfile + rename)
- If process crashes mid-write, file is corrupted (partial JSON)
- Next startup: `load_json()` raises JSONDecodeError, application won't start

**Fix**: Use tempfile + `os.replace()` for atomic write.

---

### 9. **LOW: Unix Socket Stale File**

**File**: hardware.py:213

**Code**:
```python
if os.path.exists(path):
    os.unlink(path)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
```

**Problem**:
- Unlink before bind is safe for normal restarts
- But if SIGKILL occurs between unlink and bind, socket is stale
- Next startup: bind fails ("Address already in use")

**Mitigation**: `unlink()` at line 213 handles most cases. Full fix: `server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)` (if UNIX sockets support it).

---

### 10. **LOW: Marquee Scroll Offset Not Clamped**

**File**: lcd_menu.py:349–374

**Code**:
```python
def _draw_menu(self):
    # ... label changes ...
    label_width = len(label) - 4  # ← can become negative
    if label_width > 0:
        self._marq_offset = (self._marq_offset + 1) % label_width
    text = label[self._marq_offset : self._marq_offset + 20]  # ← potential IndexError
```

**Problem**:
- If label shrinks mid-scroll, `_marq_offset` can exceed length
- `label[offset:offset+width]` doesn't error, but produces truncated string

**Severity**: Very low (string slicing is safe in Python).

**Fix**: Clamp offset when label changes: `self._marq_offset = min(self._marq_offset, max(0, len(label) - 4))`.

---

## 12. Summary of Recommended Fixes

### Priority 1 (Critical)
1. **Cup removal timeout**: Raise HardwareError or force `_cup_present = False` after timeout
2. **Order re-polling**: Implement de-duplication (track `last_order_ids`) and move failed orders to "on-hold"

### Priority 2 (High)
3. **Servo forbidden zone**: Raise HardwareError instead of silently returning
4. **Thread safety**: Protect `_cup_present` with explicit lock or use Queue

### Priority 3 (Medium)
5. **_sync() timeout**: Raise HardwareError instead of continuing
6. **Polling loop recovery**: Add auto-resume timeout or fallback behavior
7. **Config file atomicity**: Use tempfile + `os.replace()`

### Priority 4 (Low)
8. **Marquee offset clamping**: Clamp offset when label changes
9. **Unix socket cleanup**: Already handled, but document behavior
10. **Heartbeat error handling**: Log and re-raise (or implement retry loop)

---

## 13. Conclusion

The barbot system is a sophisticated embedded application with:
- ✅ Modular architecture with clear separation of concerns
- ✅ Comprehensive error logging with timestamps
- ✅ Crash-safe order progress (disk-persisted)
- ✅ Safety-critical servo logic (servo always closed before stepper moves)
- ⚠️ Several edge-case failure modes (cup timeout, order re-polling, silent failures)
- ⚠️ Missing thread synchronization for shared state
- ⚠️ Non-atomic file writes

**Overall Assessment**: The system is **production-capable** but has critical bugs in error recovery paths that should be fixed before deployment. The most dangerous issue is the cup removal timeout, which can cause the next order to dispense into the wrong cup.
