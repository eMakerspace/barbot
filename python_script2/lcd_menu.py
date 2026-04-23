#!/usr/bin/env python3
"""
Barbot LCD Menu  –  replaces console.py
────────────────────────────────────────
Hardware:
  LCD  : 2004A via I2C PCF8574 backpack @ 0x3F
  CLK  → BCM 27 (BOARD 13)
  DT   → BCM 17 (BOARD 11)
  SW   → BCM 22 (BOARD 15)

Controls:
  Rotate → navigate menu / scroll info
  Press  → enter / confirm / dismiss
  (In Run mode: Press stops the polling loop)
"""

import threading
import time
import sys

import RPi.GPIO as GPIO

from config import BarbotConfig, AttributesConfig, SPIRIT_SLOTS, MIXER_SLOTS, ALL_SLOTS
from store import StoreConfig
from woo_client import WooClient
from hardware import HardwareInterface
from inventory import InventoryManager
from orders import OrderProcessor
from lcd import LcdDisplay, COLS, ROWS
from encoder import RotaryEncoder
from logger import log_debug, log_info, log_warn, log_error, log_critical

# ── GPIO pin assignments ──────────────────────────────────────
GPIO_CLK = 27   # BCM (BOARD 13)
GPIO_DT  = 17   # BCM (BOARD 11)
GPIO_SW  = 22   # BCM (BOARD 15)

VISIBLE  = 3    # item rows; row 0 is always the header

SPINNER  = ('|', '/', '-', '\\')
ARROW    = '>'      # selection indicator (works in all charmaps)

# ── PIN Lock feature toggle ───────────────────────────────────
# Set to False to disable PIN lock, True to enable it
ENABLE_PIN_LOCK = False
PIN_CODE = '6969'

# ── Layout constants ──────────────────────────────────────────
# Each menu row: SEL(1) SP(1) LABEL(16) HINT(2) = 20
LABEL_W  = 16
HINT_W   = 2


class LCDMenu:
    """
    Full-featured 4×20 LCD + rotary-encoder menu that replaces Console.
    Identical public interface: same constructor signature, same run().
    """

    def __init__(
        self,
        config: BarbotConfig,
        store: StoreConfig,
        attributes: AttributesConfig,
        woo: WooClient,
        hardware: HardwareInterface,
        inventory: InventoryManager,
        orders: OrderProcessor,
    ):
        self.config     = config
        self.store      = store
        self.attributes = attributes
        self.woo        = woo
        self.hw         = hardware
        self.inventory  = inventory
        self.orders     = orders

        self._lcd     = LcdDisplay()
        self._encoder = RotaryEncoder(
            GPIO_CLK, GPIO_DT, GPIO_SW,
            on_rotate=self._on_rotate,
            on_press=self._on_press,
        )

        self._lock      = threading.Lock()
        self._alive     = True
        self._dirty     = True
        self._spin_idx  = 0

        # ── Navigation stack ─────────────────────────────────
        # Each entry: (title_fn, items_fn)
        #   title_fn() -> str
        #   items_fn() -> list of {'label', 'hint'(2 chars), 'action'(callable|None)}
        self._stack: list = []
        self._nav:   list = []     # parallel [cursor, scroll] per stack entry

        # ── Mode ─────────────────────────────────────────────
        # 'menu' | 'info' | 'working' | 'confirm' | 'run' | 'visc_edit'
        # | 'x_move' | 'num_entry' | 'teach' | 'mixing' | 'pin_lock'
        self._mode = 'menu'
        self._locked = True
        self._pin_entry: list[str] = []
        self._pin_cur = 0
        self._pin_error_until = 0.0
        self._last_activity = time.time()

        # visc_edit state
        self._vedit_bottle = ''
        self._vedit_data:  dict = {}
        self._vedit_val    = 1.0

        # x_move state
        self._xmove_val    = 0       # current target position
        self._xmove_step   = 1       # last computed acceleration step
        self._xmove_last_t = 0.0     # timestamp of last rotation event

        # teach state
        self._teach_slot   = ''      # slot being taught
        self._teach_val    = 0       # jogged target step position
        self._teach_step   = 1
        self._teach_last_t = 0.0
        self._teach_timer: threading.Timer | None = None  # debounce timer

        # num_entry state (generic integer input screen)
        self._nentry_title    = ''
        self._nentry_subtitle = ''
        self._nentry_field    = ''
        self._nentry_val      = 0
        self._nentry_min      = 0
        self._nentry_max      = 100
        self._nentry_step     = 1
        self._nentry_fast     = None  # fast step size; None = no acceleration
        self._nentry_last_t   = 0.0
        self._nentry_cb       = None

        # info state
        self._info_title  = ''
        self._info_lines: list[str] = []
        self._info_scroll = 0

        # working state
        self._work_title   = ''
        self._work_done    = False
        self._work_result  = ''
        self._work_dismiss = False   # guard: only one auto-dismiss thread

        # confirm state
        self._conf_title       = ''
        self._conf_msg         = ''
        self._conf_yn          = 0      # 0 = YES, 1 = NO
        self._conf_cb          = None
        self._conf_cancel_mode = 'menu' # mode to restore when NO is chosen

        # run (polling) state
        self._run_stop:            threading.Event | None = None
        self._run_count            = 0
        self._run_last_id          = None
        self._run_backlight_until  = 0.0   # epoch time; backlight on while now < this
        self._polling_paused       = False # pause polling when error occurs
        self._error_acked          = False # user acknowledged error
        self._retry_drink          = False # retry failed drink when user presses button

        # mixing state (shown during active drink dispensing)
        self._mix_drink_num  = 0
        self._mix_total      = 0
        self._mix_name       = ''

        # cup removed / add cup state
        self._cup_removed_time = 0.0   # time when cup_removed mode was shown

        # marquee (ping-pong scroll for selected row)
        self._marq_label  = ''
        self._marq_offset = 0
        self._marq_dir    = 1
        self._marq_next   = 0.0
        self._marq_pause  = 0.0

    # ══════════════════════════════════════════════════════════
    # Public entry point
    # ══════════════════════════════════════════════════════════

    def run(self):
        log_info("LCDUI", "LCD Menu starting...")
        try:
            log_info("LCDUI", "Initializing GPIO...")
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)

            log_info("LCDUI", "Clearing LCD and starting encoder...")
            self._lcd.clear()
            self._encoder.start()

            # If serial is unavailable (e.g. locked by barbot_web.py), show a
            # prominent warning and wait for the user to press the encoder button
            # before continuing in simulation mode.
            if self.hw.serial_error:
                log_warn("LCDUI", f"Serial error detected: {self.hw.serial_error}")
                self._show_serial_error(self.hw.serial_error)

            # Startup sequence: home first (blocking), then go straight to menu.
            # The WooCommerce fetch runs in the background — it can take a long
            # time (or hang) if the server is unreachable and must not block boot.
            log_info("LCDUI", "Beginning homing sequence...")
            self._begin_work('Homing...', self._do_homing)
            self._wait_work()
            log_info("LCDUI", "Homing complete")

            log_info("LCDUI", "Normalizing slot mappings...")
            self._normalize_slots()

            # Fire fetch in background — errors are swallowed, user can re-fetch
            # from Setup → Fetch once the machine is ready.
            log_info("LCDUI", "Starting background fetch thread...")
            threading.Thread(target=self._bg_fetch, daemon=True).start()

            # Main menu
            log_info("LCDUI", "Pushing main menu...")
            self._push(self._title_main, self._items_main)
            with self._lock:
                # Engage PIN lock only if enabled
                if ENABLE_PIN_LOCK:
                    self._engage_pin_lock_locked(reset_to_root=True)

            log_info("LCDUI", "Entering render loop...")
            try:
                while self._alive:
                    self._render()
                    time.sleep(0.05)
            except KeyboardInterrupt:
                log_critical("LCDUI", "KeyboardInterrupt caught in render loop")
                raise
        except KeyboardInterrupt:
            log_critical("LCDUI", "KeyboardInterrupt caught, initiating cleanup...")
            # Cleanup will happen in finally block below
        except Exception as e:
            log_error("LCDUI", f"Exception in LCD menu: {type(e).__name__}: {e}")
            try:
                self._lcd.clear()
                self._write_row(0, 'Error!')
                self._write_row(1, str(e)[:20])
                time.sleep(2.0)
            except Exception as display_err:
                log_warn("LCDUI", f"Could not display error: {display_err}")
        finally:
            log_info("LCDUI", "LCD Menu cleanup starting...")
            try:
                log_debug("LCDUI", "Stopping encoder...")
                self._encoder.stop()
                log_info("LCDUI", "Encoder stopped")
            except Exception as e:
                log_warn("LCDUI", f"Encoder stop error: {e}")

            try:
                log_debug("LCDUI", "Clearing LCD and displaying goodbye...")
                self._lcd.clear()
                self._write_row(1, '      Goodbye!      ')
                time.sleep(1.0)
                self._lcd.clear()
                self._lcd.backlight = False
                log_info("LCDUI", "LCD cleared")
            except Exception as e:
                log_warn("LCDUI", f"LCD cleanup error: {e}")

            try:
                log_debug("LCDUI", "Cleaning up GPIO...")
                GPIO.cleanup()
                log_info("LCDUI", "GPIO cleanup complete")
            except Exception as e:
                log_warn("LCDUI", f"GPIO cleanup error: {e}")

            log_critical("LCDUI", "LCD Menu shutdown complete")

    # ══════════════════════════════════════════════════════════
    # LCD helpers
    # ══════════════════════════════════════════════════════════

    def _write_row(self, r: int, text: str):
        self._lcd.write_row(r, text)

    @staticmethod
    def _hdr(title: str, up: bool = False, dn: bool = False) -> str:
        """Build a 20-char header: ={u}={title:^14}={d}="""
        u     = '^' if up else '='
        d     = 'v' if dn else '='
        inner = f'{title[:14]:^14}'
        return f'={u}={inner}={d}='   # 1+1+1+14+1+1+1 = 20

    # ══════════════════════════════════════════════════════════
    # Navigation stack
    # ══════════════════════════════════════════════════════════

    def _push(self, title_fn, items_fn, custom_state=None):
        """Push a new menu level. custom_state allows storing mutable state for custom modes."""
        with self._lock:
            self._stack.append((title_fn, items_fn))
            self._nav.append([0, 0])
            self._mode  = 'menu'
            self._dirty = True
            if custom_state:
                if not hasattr(self, '_custom_state_stack'):
                    self._custom_state_stack = []
                self._custom_state_stack.append(custom_state)
        self._marq_reset()

    def _pop(self):
        with self._lock:
            if len(self._stack) > 1:
                self._stack.pop()
                self._nav.pop()
                if hasattr(self, '_custom_state_stack') and self._custom_state_stack:
                    self._custom_state_stack.pop()
            self._mode  = 'menu'
            self._dirty = True
        self._marq_reset()

    def _nav_cur(self) -> list:
        return self._nav[-1]

    def _items_cur(self) -> list:
        return self._stack[-1][1]()

    def _title_cur(self) -> str:
        return self._stack[-1][0]()

    # ══════════════════════════════════════════════════════════
    # Marquee engine (ping-pong scroll, selected row only)
    # ══════════════════════════════════════════════════════════

    def _marq_reset(self):
        self._marq_label  = ''
        self._marq_offset = 0
        self._marq_dir    = 1
        self._marq_next   = time.time() + 0.4
        self._marq_pause  = time.time() + 1.2

    def _marq_tick(self, label: str, width: int) -> str:
        """
        Return a `width`-char window into `label`.
        Short labels: returned as-is (padded).
        Long labels: smooth ping-pong scroll with pause at each end.
        Sets _dirty=True whenever the offset advances so the render loop
        picks it up automatically.
        """
        if len(label) <= width:
            return f'{label:<{width}}'

        if label != self._marq_label:
            self._marq_label  = label
            self._marq_offset = 0
            self._marq_dir    = 1
            self._marq_pause  = 0.0  # No pause initially
            self._marq_next   = time.time() + 0.4

        now = time.time()
        if now >= self._marq_next:
            # Only pause at the ends, not while scrolling
            if now < self._marq_pause:
                return label[self._marq_offset: self._marq_offset + width]
            self._marq_next = now + 0.35
            max_off = len(label) - width
            self._marq_offset += self._marq_dir
            if self._marq_offset >= max_off:
                self._marq_offset = max_off
                self._marq_dir    = -1
                self._marq_pause  = now + 1.0
            elif self._marq_offset <= 0:
                self._marq_offset = 0
                self._marq_dir    = 1
                self._marq_pause  = now + 1.0
            # Signal that display needs updating
            with self._lock:
                self._dirty = True

        return label[self._marq_offset: self._marq_offset + width]

    # ══════════════════════════════════════════════════════════
    # Input handlers  (called from RotaryEncoder HAL thread)
    # ══════════════════════════════════════════════════════════

    def _on_rotate(self, d: int):
        with self._lock:
            self._last_activity = time.time()
            m = self._mode

            if m == 'pin_lock':
                self._pin_cur = (self._pin_cur + d) % 10
                self._dirty = True

            elif m == 'run':
                # Any rotation wakes the backlight for 5 seconds
                self._run_backlight_until = time.time() + 5.0
                self._lcd.backlight = True

            elif m == 'visc_edit':
                self._vedit_val = round(self._vedit_val + d * 0.1, 1)
                self._vedit_val = max(0.1, min(10.0, self._vedit_val))
                self._dirty = True

            elif m == 'x_move':
                now = time.time()
                dt  = now - self._xmove_last_t
                self._xmove_last_t = now
                # Two speeds: slow rotation = 1, fast rotation = 100
                step = 100 if dt < 0.01 else 1
                self._xmove_step = step
                self._xmove_val  = max(0,
                                   min(self.hw.hw.x_max, self._xmove_val + d * step))
                self._dirty = True

            elif m == 'teach':
                now = time.time()
                dt  = now - self._teach_last_t
                self._teach_last_t = now
                step = 100 if dt < 0.01 else 1
                self._teach_step = step
                self._teach_val  = max(0,
                                   min(self.hw.hw.x_max, self._teach_val + d * step))
                self._dirty = True
                # Cancel any queued move and schedule a fresh one after 250 ms
                # of silence — only the final resting position gets sent.
                if self._teach_timer is not None:
                    self._teach_timer.cancel()
                target = self._teach_val
                self._teach_timer = threading.Timer(
                    0.25, lambda t=target: self.hw._queue_move(t)
                )
                self._teach_timer.start()

            elif m == 'num_entry':
                now = time.time()
                dt  = now - self._nentry_last_t
                self._nentry_last_t = now
                s = (self._nentry_fast if dt < 0.05 else self._nentry_step) \
                    if self._nentry_fast else self._nentry_step
                self._nentry_val = max(self._nentry_min,
                                   min(self._nentry_max, self._nentry_val + d * s))
                self._dirty = True

            elif m == 'menu' and self._stack:
                items = self._items_cur()
                nav   = self._nav_cur()
                old   = nav[0]
                nav[0] = max(0, min(len(items) - 1, nav[0] + d))
                if nav[0] != old:
                    self._marq_label = ''
                if nav[0] < nav[1]:
                    nav[1] = nav[0]
                elif nav[0] >= nav[1] + VISIBLE:
                    nav[1] = nav[0] - VISIBLE + 1
                self._dirty = True

            elif m == 'info':
                max_s = max(0, len(self._info_lines) - (ROWS - 1))
                self._info_scroll = max(0, min(max_s, self._info_scroll + d))
                self._dirty = True

            elif m == 'error':
                if self._polling_paused:
                    self._retry_drink = not self._retry_drink
                    self._dirty = True

            elif m == 'confirm':
                self._conf_yn = 1 - self._conf_yn
                self._dirty = True

    def _on_press(self):
        """Handle button press. Action is called OUTSIDE the lock."""
        action = None

        with self._lock:
            self._last_activity = time.time()
            m = self._mode

            if m == 'pin_lock':
                self._pin_entry.append(str(self._pin_cur))
                if len(self._pin_entry) >= len(PIN_CODE):
                    entered = ''.join(self._pin_entry[:len(PIN_CODE)])
                    if entered == PIN_CODE:
                        self._locked = False
                        self._pin_entry = []
                        self._pin_cur = 0
                        self._pin_error_until = 0.0
                        self._mode = 'menu'
                        self._dirty = True
                    else:
                        self._pin_entry = []
                        self._pin_cur = 0
                        self._pin_error_until = time.time() + 1.5
                        self._dirty = True
                else:
                    self._pin_cur = 0
                    self._dirty = True

            elif m == 'working':
                if self._work_done:
                    self._mode  = 'menu'
                    self._dirty = True

            elif m == 'info':
                self._mode  = 'menu'
                self._dirty = True

            elif m == 'visc_edit':
                # Press → show save/discard confirm; NO returns to edit screen
                bottle = self._vedit_bottle
                data   = self._vedit_data
                val    = self._vedit_val
                action = lambda b=bottle, d=data, v=val: self._show_confirm(
                    'Viscosity',
                    f'Save {v:.1f}?',
                    lambda: self._do_set_viscosity(b, d, v),
                    cancel_mode='menu',
                )

            elif m == 'x_move':
                pos    = self._xmove_val
                action = lambda p=pos: self._show_confirm(
                    'Move X',
                    f'Move to {p}?',
                    lambda: self._do_move_x(p),
                    cancel_mode='menu',
                )

            elif m == 'teach':
                slot = self._teach_slot
                pos  = self._teach_val
                action = lambda s=slot, p=pos: self._show_confirm(
                    f'Save {s}',
                    f'Save pos {p}?',
                    lambda: self._do_teach_save(s, p),
                    cancel_mode='teach',
                )

            elif m == 'num_entry':
                val   = self._nentry_val
                cb    = self._nentry_cb
                title = self._nentry_title
                action = lambda v=val, c=cb, t=title: self._show_confirm(
                    t,
                    f'Confirm: {v}?',
                    lambda: self._do_num_entry(c, v),
                    cancel_mode='menu',
                )

            elif m == 'confirm':
                yn, cb        = self._conf_yn, self._conf_cb
                cancel_mode   = self._conf_cancel_mode
                self._mode    = 'menu' if yn == 0 else cancel_mode
                self._dirty   = True
                if yn == 0:
                    action = cb

            elif m == 'run':
                # Light up backlight for the confirm dialog
                self._lcd.backlight = True
                action = lambda: self._show_confirm(
                    'Stop Polling',
                    'Stop polling?',
                    self._do_stop_run,
                    cancel_mode='run',
                )

            elif m == 'error':
                if self._polling_paused:
                    # Press confirms the current RETRY/CANCEL choice and resumes polling
                    retry = self._retry_drink
                    self._polling_paused = False
                    self._error_acked = True
                    self._mode = 'run'
                    self._dirty = True
                    if retry:
                        log_info("LCDUI", "User confirmed RETRY — retrying mixers")
                    else:
                        log_info("LCDUI", "User confirmed CANCEL — resuming polling")
                else:
                    self._mode = 'menu'
                    self._dirty = True

            elif m == 'menu' and self._stack:
                items = self._items_cur()
                nav   = self._nav_cur()
                if items:
                    action = items[nav[0]].get('action')

        if action:
            action()

    # ══════════════════════════════════════════════════════════
    # Mode helpers
    # ══════════════════════════════════════════════════════════

    def _show_serial_error(self, error: str):
        """Block on LCD until the user presses the encoder button.

        Shows a flashing WARNING banner so the operator knows the machine is
        running in simulation mode (no ESP32 connected / port locked).
        """
        # Pick a short 2nd-line hint based on the error type
        if 'locked' in error.lower():
            hint1 = 'Port locked!'
            hint2 = 'Stop barbot_web.py'
        elif 'configured' in error.lower():
            hint1 = 'No port configured'
            hint2 = 'Check hw config'
        else:
            hint1 = 'ESP32 unavailable'
            hint2 = error[:20]

        def _draw(flash: bool):
            self._lcd.clear()
            self._write_row(0, '!!!! WARNING !!!!!!!' if flash else '====================')
            self._write_row(1, 'SIM MODE - NO ESP32 ')
            self._write_row(2, f'{hint1:<20}')
            self._write_row(3, f'{hint2:<20}')

        pressed = threading.Event()
        orig_press = self._encoder._on_press

        def _dismiss():
            pressed.set()

        self._encoder._on_press = _dismiss

        flash = True
        while not pressed.wait(timeout=0.5):
            _draw(flash)
            flash = not flash

        # Show static screen briefly so user sees it registered
        _draw(False)
        self._write_row(3, '[Press to continue] ')
        time.sleep(1.0)

        self._encoder._on_press = orig_press
        self._lcd.clear()

    def _begin_work(self, title: str, fn, *args):
        """Switch to 'working' mode and run fn(*args) in a daemon thread."""
        with self._lock:
            self._mode          = 'working'
            self._work_title    = title
            self._work_done     = False
            self._work_result   = ''
            self._work_dismiss  = False
            self._dirty         = True

        def _run():
            try:
                res = fn(*args)
                result = res if isinstance(res, str) else 'Done!'
            except Exception as e:
                result = f'Err: {str(e)[:14]}'
            with self._lock:
                self._work_result  = result
                self._work_done    = True
                self._dirty        = True

        threading.Thread(target=_run, daemon=True).start()

    def _wait_work(self):
        """Block the CALLING thread until working mode finishes (startup only)."""
        while True:
            self._render()
            with self._lock:
                if self._work_done:
                    self._mode = 'menu'
                    break
            time.sleep(0.05)

    def _show_info(self, title: str, lines: list):
        with self._lock:
            self._mode        = 'info'
            self._info_title  = title
            self._info_lines  = [str(ln)[:COLS] for ln in lines]
            self._info_scroll = 0
            self._dirty       = True

    def _show_confirm(self, title: str, msg: str, callback, cancel_mode: str = 'menu'):
        with self._lock:
            self._mode             = 'confirm'
            self._conf_title       = title
            self._conf_msg         = msg[:COLS]
            self._conf_yn          = 0
            self._conf_cb          = callback
            self._conf_cancel_mode = cancel_mode
            self._dirty            = True

    def _engage_pin_lock_locked(self, *, reset_to_root: bool):
        self._locked = True
        self._mode = 'pin_lock'
        self._pin_entry = []
        self._pin_cur = 0
        self._pin_error_until = 0.0
        self._dirty = True
        if reset_to_root and self._stack:
            self._stack = self._stack[:1]
            self._nav = self._nav[:1]
            if self._nav:
                self._nav[0] = [0, 0]
            if hasattr(self, '_custom_state_stack') and self._custom_state_stack:
                self._custom_state_stack = self._custom_state_stack[:1]
            self._marq_label = ''
        self._lcd.backlight = True

    # ══════════════════════════════════════════════════════════
    # Render dispatcher
    # ══════════════════════════════════════════════════════════

    def _render(self):
        with self._lock:
            mode  = self._mode
            dirty = self._dirty
            # 'working', 'run', 'mixing', 'menu', and 'pin_lock' always redraw
            # (menu needs it for marquee; row cache prevents redundant I2C writes)
            if mode not in ('working', 'run', 'mixing', 'menu', 'pin_lock', 'cup_removed', 'add_cup', 'error') and not dirty:
                return
            self._dirty = False

        if   mode == 'menu':      self._draw_menu()
        elif mode == 'pin_lock':  self._draw_pin_lock()
        elif mode == 'info':      self._draw_info()
        elif mode == 'working':   self._draw_working()
        elif mode == 'confirm':   self._draw_confirm()
        elif mode == 'run':       self._draw_run()
        elif mode == 'mixing':    self._draw_mixing()
        elif mode == 'cup_removed': self._draw_cup_removed()
        elif mode == 'add_cup':   self._draw_add_cup()
        elif mode == 'error':     self._draw_error()
        elif mode == 'visc_edit': self._draw_visc_edit()
        elif mode == 'x_move':    self._draw_x_move()
        elif mode == 'num_entry': self._draw_num_entry()
        elif mode == 'teach':     self._draw_teach()

    # ── Individual draw methods ───────────────────────────────

    def _draw_menu(self):
        if not self._stack:
            return
        items      = self._items_cur()
        nav        = self._nav_cur()
        cur, scr   = nav[0], nav[1]
        n          = len(items)

        self._write_row(0, self._hdr(self._title_cur(), scr > 0, scr + VISIBLE < n))

        for row in range(VISIBLE):
            idx = scr + row
            if idx >= n:
                self._write_row(row + 1, '')
                continue
            item  = items[idx]
            sel   = ARROW if idx == cur else ' '
            hint  = f'{item.get("hint", "  "):<2}'[:2]
            label = item.get('label', '')
            # Marquee only on the selected item if it overflows
            if idx == cur and len(label) > LABEL_W:
                vis = self._marq_tick(label, LABEL_W)
            else:
                vis = f'{label[:LABEL_W]:<{LABEL_W}}'
            self._write_row(row + 1, f'{sel} {vis}{hint}')

    def _draw_info(self):
        with self._lock:
            title  = self._info_title
            lines  = self._info_lines[:]
            scroll = self._info_scroll
        n = len(lines)
        self._write_row(0, self._hdr(title, scroll > 0, scroll + (ROWS - 1) < n))
        for r in range(1, ROWS):
            li   = scroll + r - 1
            text = lines[li] if li < n else ''
            self._write_row(r, text)

    def _draw_working(self):
        with self._lock:
            title   = self._work_title
            done    = self._work_done
            result  = self._work_result
            dismiss = self._work_dismiss
        self._spin_idx = (self._spin_idx + 1) % len(SPINNER)
        spin = SPINNER[self._spin_idx]

        if not done:
            self._write_row(0, '=== Working... =====')
            self._write_row(1, f' {spin} {title[:17]:<17}')
            self._write_row(2, '                    ')
            self._write_row(3, '                    ')
        else:
            self._write_row(0, '==== Complete! =====')
            self._write_row(1, f' {result[:18]:<18}')
            self._write_row(2, '                    ')
            self._write_row(3, '  [Press to return] ')
            # Auto-dismiss after 2 s; guard prevents duplicate threads
            if not dismiss:
                with self._lock:
                    self._work_dismiss = True
                threading.Thread(target=self._work_auto_dismiss, daemon=True).start()

    def _work_auto_dismiss(self):
        time.sleep(2.0)
        with self._lock:
            if self._mode == 'working' and self._work_done:
                self._mode  = 'menu'
                self._dirty = True

    def _draw_confirm(self):
        with self._lock:
            title = self._conf_title
            msg   = self._conf_msg
            yn    = self._conf_yn
        yes_sel = ARROW if yn == 0 else ' '
        no_sel  = ARROW if yn == 1 else ' '
        self._write_row(0, self._hdr(title))
        self._write_row(1, f'{msg:<20}'[:20])
        self._write_row(2, f' {yes_sel}YES      {no_sel}NO')
        self._write_row(3, '   [Press confirm]  ')

    def _draw_visc_edit(self):
        with self._lock:
            bottle = self._vedit_bottle
            val    = self._vedit_val
        self._write_row(0, self._hdr(f'{bottle[:14]} Adj'))
        self._write_row(1, f'  Viscosity: {val:<6.1f}  ')
        self._write_row(2, '  Rotate to adjust  ')
        self._write_row(3, '  Press to save     ')

    def _draw_x_move(self):
        with self._lock:
            val  = self._xmove_val
            step = self._xmove_step
        cur = self.hw.x_position
        self._write_row(0, self._hdr('Move X'))
        self._write_row(1, f'  Current:  {cur:>5}     ')
        self._write_row(2, f'  Target:   {val:>5}     ')
        self._write_row(3, f'  Step:{step:>4}  [Press] ')

    def _draw_teach(self):
        with self._lock:
            slot = self._teach_slot
            val  = self._teach_val
            step = self._teach_step
        saved = self.hw.hw.slot_positions.get(slot, '?')
        self._write_row(0, self._hdr(f'Teach {slot}'))
        self._write_row(1, f'  Saved:   {str(saved):>6}     ')
        self._write_row(2, f'  Target:  {val:>6}     ')
        self._write_row(3, f'  Stp:{step:>4}  [Save] ')

    def _draw_num_entry(self):
        with self._lock:
            title    = self._nentry_title
            subtitle = self._nentry_subtitle
            field    = self._nentry_field
            val      = self._nentry_val
        self._write_row(0, self._hdr(title))
        self._write_row(1, f'  {subtitle:<18}')
        self._write_row(2, f'  {field:<10}{val:>6}  ')
        self._write_row(3, '  [Press to confirm] ')

    def _draw_pin_lock(self):
        with self._lock:
            entered = len(self._pin_entry)
            cur = self._pin_cur
            err_until = self._pin_error_until

        cells = []
        for idx in range(len(PIN_CODE)):
            if idx < entered:
                cells.append('*')
            elif idx == entered:
                cells.append(str(cur))
            else:
                cells.append('_')
        pin_line = f' PIN: {" ".join(cells)}'

        self._write_row(0, self._hdr('Locked'))
        self._write_row(1, f'{pin_line:<20}'[:20])
        self._write_row(2, ' Rotate: set digit  ')
        if time.time() < err_until:
            self._write_row(3, '   Incorrect PIN    ')
        else:
            self._write_row(3, ' Press: next digit  ')

    def _draw_run(self):
        with self._lock:
            count    = self._run_count
            last_id  = self._run_last_id
            bl_until = self._run_backlight_until
        # Backlight timeout: turn off once 5 s after last rotation has elapsed
        if time.time() >= bl_until:
            self._lcd.backlight = False
        self._spin_idx = (self._spin_idx + 1) % len(SPINNER)
        spin = SPINNER[self._spin_idx]
        last_str = f'#{last_id}' if last_id else 'none'
        self._write_row(0, self._hdr(f'{spin} Polling'))
        self._write_row(1, f' Orders done: {count:<6}')
        self._write_row(2, f' Last: {last_str:<13}')
        self._write_row(3, '  [Press to stop]   ')

    def _draw_mixing(self):
        with self._lock:
            num   = self._mix_drink_num
            total = self._mix_total
            name  = self._mix_name
        self._spin_idx = (self._spin_idx + 1) % len(SPINNER)
        spin = SPINNER[self._spin_idx]
        progress = f'{num}/{total}'
        vis = self._marq_tick(name, 17)
        self._write_row(0, self._hdr(f'Mixing {progress}'))
        self._write_row(1, f' {spin} {vis}')
        self._write_row(2, '                    ')
        self._write_row(3, '                    ')

    def _draw_cup_removed(self):
        """Show brief 'cup removed' confirmation (auto-clears after ~2s)."""
        with self._lock:
            elapsed = time.time() - self._cup_removed_time
            if elapsed > 2.0:
                self._mode = 'add_cup'
                self._dirty = True
                return
        self._write_row(0, self._hdr('BarBot'))
        self._write_row(1, '  Enjoy your    ')
        self._write_row(2, '    drink!      ')
        self._write_row(3, '                ')

    def _draw_add_cup(self):
        """Show flashing 'PUT CUP IN' prompt."""
        flash_on = (int(time.time() * 4) % 2) == 0  # fast flash
        self._write_row(0, self._hdr('BarBot'))
        self._write_row(1, ' Waiting for cup ')
        self._write_row(2, '   PUT CUP IN!   ' if flash_on else '                ')
        self._write_row(3, '                ')

    def _draw_error(self):
        """Show error state from ESP32."""
        with self._lock:
            error_name = getattr(self, '_error_name', 'Unknown Error')
            severity = getattr(self, '_error_severity', 0)
            error_time = getattr(self, '_error_time', time.time())
            polling_paused = self._polling_paused
            retry_drink = self._retry_drink

        elapsed = time.time() - error_time
        severity_names = ['INFO', 'WARN', 'ERROR', 'CRITICAL']
        sev_str = severity_names[min(severity, 3)]

        # Flash for critical errors
        flash = (int(elapsed * 2) % 2) if severity >= 3 else 0
        header = '!!!! ERROR !!!!!!!' if flash else 'ERROR:'

        self._write_row(0, header[:20])

        if polling_paused:
            # Rows 1-3: error name + side-by-side RETRY/CANCEL selector
            r = ARROW if retry_drink else ' '
            c = ARROW if not retry_drink else ' '
            self._write_row(1, f'{error_name:<20}')
            self._write_row(2, f' {r}RETRY     {c}CANCEL')
            self._write_row(3, '   [Press confirm]  ')
        else:
            self._write_row(1, f'[{sev_str}]')
            self._write_row(2, f'{error_name:<20}')
            self._write_row(3, f'T:{elapsed:.0f}s')

        # Auto-dismiss non-critical errors after 5 seconds if polling not paused
        if severity < 3 and elapsed > 5.0 and not polling_paused:
            with self._lock:
                self._mode = 'menu'
                self._dirty = True

    # ── Public hooks for order processor ─────────────────────────

    def show_mixing(self, drink_num: int, total: int, name: str):
        """Called by OrderProcessor before dispensing each drink."""
        with self._lock:
            self._mix_drink_num = drink_num
            self._mix_total     = total
            self._mix_name      = name
            self._mode          = 'mixing'
            self._dirty         = True
        self._marq_reset()
        self._lcd.backlight = True

    def clear_mixing(self):
        """Called by OrderProcessor after all drinks in an order are done."""
        with self._lock:
            self._mode  = 'run'
            self._dirty = True
        # Restore run-mode backlight behaviour (off until rotated)
        self._lcd.backlight = False

    def show_cup_removed(self):
        """Show brief 'cup removed' confirmation message."""
        with self._lock:
            self._mode = 'cup_removed'
            self._cup_removed_time = time.time()
            self._dirty = True
        self._lcd.backlight = True

    def show_add_cup(self):
        """Show 'place new cup' prompt."""
        with self._lock:
            self._mode = 'add_cup'
            self._dirty = True
        self._lcd.backlight = True

    def show_error(self, error_name: str, severity: int):
        """Show error message from ESP32 on LCD.

        Args:
            error_name: Human-readable error name (e.g., 'Pump Failure')
            severity: Error severity level (0=info, 1=warning, 2=error, 3=critical)
        """
        with self._lock:
            self._mode = 'error'
            self._error_name = error_name[:20]
            self._error_severity = severity
            self._error_time = time.time()
            # Errors severe enough to stop dispensing need user acknowledgement
            if severity >= 2:
                self._polling_paused = True
                self._error_acked = False
            self._dirty = True
        self._lcd.backlight = True

    def pause_polling(self, error_name: str = None):
        """Pause polling when an error occurs and wait for user intervention.
        
        Args:
            error_name: Optional error name to display (uses last error if not provided)
        """
        with self._lock:
            self._polling_paused = True
            self._error_acked = False
            if error_name:
                self._error_name = error_name[:20]
                self._error_severity = 2  # Error severity
                self._error_time = time.time()
            self._mode = 'error'
            self._dirty = True
        self._lcd.backlight = True
        log_info("LCDUI", "Polling paused - waiting for user intervention")

    def resume_polling(self):
        """Resume polling after user acknowledges error."""
        with self._lock:
            self._polling_paused = False
            self._error_acked = True
            self._mode = 'run'
            self._dirty = True
        log_info("LCDUI", "Polling resumed by user")

    # ══════════════════════════════════════════════════════════
    # Startup tasks
    # ══════════════════════════════════════════════════════════

    def _do_homing(self) -> str:
        self.hw.homing()
        return 'Homing complete!'

    def _do_tare_scale(self) -> str:
        return self.hw.tare_scale()

    def _do_calibrate_scale(self, known_grams: int) -> str:
        return self.hw.calibrate_scale(float(known_grams))

    def _do_read_weight(self) -> str:
        return self.hw.read_weight_str()

    def _do_emergency_stop(self) -> str:
        self.hw.emergency_stop()
        return 'Stopped! Press Resume.'

    def _do_resume(self) -> str:
        self.hw.resume()
        return 'Resumed.'

    def _do_fetch(self) -> str:
        self._fetch_attributes()
        self.store.fetch(self.woo, self.attributes.term_slugs)
        return 'Store loaded!'

    def _bg_fetch(self):
        """Silent background fetch at startup — swallows all errors."""
        try:
            self._do_fetch()
        except Exception as e:
            print(f"[UI] Background fetch failed: {e}")

    def _normalize_slots(self):
        """Convert display names in slot_mapping to attribute slugs."""
        name_to_slug = {
            name: slug
            for terms in self.attributes.term_slugs.values()
            for name, slug in terms.items()
        }
        if not name_to_slug:
            return
        changed = any(
            ing in name_to_slug
            for ing in self.config.slot_mapping.values()
        )
        if changed:
            for slot, ing in list(self.config.slot_mapping.items()):
                if ing in name_to_slug:
                    self.config.slot_mapping[slot] = name_to_slug[ing]
            self.config.save()

    def _fetch_attributes(self):
        """Fetch WooCommerce attributes and bottle properties."""
        try:
            attributes = self.woo.fetch_all('products/attributes')
        except Exception:
            return

        attr_ids, attr_slugs, term_slugs, bottle_props = {}, {}, {}, {}

        for attr in attributes:
            aid  = attr.get('id')
            anam = attr.get('name')
            aslg = attr.get('slug')
            if not all((aid, anam, aslg)):
                continue
            attr_ids[aslg]  = aid
            attr_slugs[anam] = aslg

            try:
                terms = self.woo.fetch_all(f'products/attributes/{aid}/terms')
            except Exception:
                continue

            term_slugs[aslg]  = {}
            bottle_props[aslg] = {}

            for term in terms:
                tid  = term.get('id')
                tnam = term.get('name')
                tslg = term.get('slug')
                if not all((tid, tnam, tslg)):
                    continue
                term_slugs[aslg][tnam] = tslg

                bp = term.get('bottle_properties', {})
                bottle_props[aslg][tslg] = {
                    'id':          tid,
                    'name':        tnam,
                    'bottle_size': float(bp.get('bottle_size', 70)),
                    'viscosity':   float(bp.get('viscosity',   1)),
                }

        self.attributes.update_attributes(attr_ids, attr_slugs, term_slugs, bottle_props)
        self.attributes.save()

    # ══════════════════════════════════════════════════════════
    # Slug ↔ display-name helpers
    # ══════════════════════════════════════════════════════════

    def _slug_maps(self) -> tuple[dict, dict]:
        spirits = {s: n for n, s in self.attributes.term_slugs.get('pa_spirits', {}).items()}
        mixers  = {s: n for n, s in self.attributes.term_slugs.get('pa_mixers',  {}).items()}
        return spirits, mixers

    def _display(self, slug: str) -> str:
        sp, mx = self._slug_maps()
        return sp.get(slug) or mx.get(slug) or slug

    def _extract_viscosity(self, attr_slug: str) -> dict:
        result = {}
        for tslg, data in self.attributes.bottle_properties.get(attr_slug, {}).items():
            result[data.get('name', tslg)] = {
                'viscosity': data.get('viscosity', 1),
                'term_id':   data.get('id'),
                'term_slug': tslg,
                'attr_slug': attr_slug,
            }
        return result

    # ══════════════════════════════════════════════════════════
    # Menu builders
    # ══════════════════════════════════════════════════════════

    # ── Main ──────────────────────────────────────────────────

    def _title_main(self) -> str:
        return 'BARBOT'

    def _items_main(self) -> list:
        return [
            {'label': 'Setup',       'hint': f'{ARROW} ', 'action': self._enter_setup},
            {'label': 'Run',         'hint': '  ',        'action': self._enter_run},
            {'label': 'Maintenance', 'hint': f'{ARROW} ', 'action': self._enter_maintenance},
            {'label': 'Quit',        'hint': '  ',        'action': self._do_quit},
        ]

    # ── Setup ──────────────────────────────────────────────────

    def _enter_setup(self):
        self._push(lambda: 'Setup', self._items_setup)

    def _items_setup(self) -> list:
        return [
            {'label': 'Fetch',     'hint': '  ',        'action': lambda: self._begin_work('Fetching...', self._do_fetch)},
            {'label': 'Status',    'hint': f'{ARROW} ', 'action': self._enter_status},
            {'label': 'Slots',     'hint': f'{ARROW} ', 'action': self._enter_slots},
            {'label': 'Viscosity', 'hint': f'{ARROW} ', 'action': self._enter_viscosity},
            {'label': 'Back',      'hint': '  ',        'action': self._pop},
        ]

    # ── Status ─────────────────────────────────────────────────

    def _enter_status(self):
        sp, mx = self._slug_maps()

        def disp(slug):
            return sp.get(slug) or mx.get(slug) or slug

        lines = ['-- Spirit Slots ----']
        for slot in SPIRIT_SLOTS:
            ing  = self.config.slot_ingredient(slot)
            flag = '[E]' if slot in self.config.empty_slots else ''
            name = disp(ing) if ing else '---'
            lines.append(f'{slot}: {name} {flag}'.rstrip())

        lines.append('-- Mixer Slots -----')
        for slot in MIXER_SLOTS:
            ing  = self.config.slot_ingredient(slot)
            flag = '[E]' if slot in self.config.empty_slots else ''
            name = disp(ing) if ing else '---'
            lines.append(f'{slot}: {name} {flag}'.rstrip())

        fetched = self.store.last_fetched or 'never'
        lines += [
            '-- Store Info ------',
            f'Fetched: {fetched}',
            f'Products: {len(self.store.products)}',
            f'Spirits:  {len(self.store.available_spirits)}',
            f'Mixers:   {len(self.store.available_mixers)}',
            '',
            '  [Press to return] ',
        ]
        self._show_info('Status', lines)

    # ── Teach slots ───────────────────────────────────────────

    def _enter_teach_slots(self):
        self._push(lambda: 'Teach Slots', self._items_teach_slots)

    def _items_teach_slots(self) -> list:
        from config import ALL_SLOTS
        items = []
        for slot in ALL_SLOTS:
            pos = self.hw.hw.slot_positions.get(slot)
            hint = f'{pos:>5}' if pos is not None else '  -- '
            items.append({
                'label': f'{slot:<10} {hint}',
                'hint': '  ',
                'action': lambda s=slot: self._enter_teach(s),
            })
        items.append({'label': 'Back', 'hint': '  ', 'action': self._pop})
        return items

    def _enter_teach(self, slot: str):
        saved = self.hw.hw.slot_positions.get(slot)

        def _move_then_teach():
            if saved is not None:
                try:
                    self.hw.move_x(saved)
                except Exception:
                    pass
            with self._lock:
                if self._teach_timer is not None:
                    self._teach_timer.cancel()
                    self._teach_timer = None
                self._teach_slot   = slot
                self._teach_val    = self.hw.x_position
                self._teach_step   = 1
                self._teach_last_t = 0.0
                self._mode         = 'teach'
                self._dirty        = True

        with self._lock:
            self._mode         = 'working'
            self._work_title   = f'Moving to {slot}'
            self._work_done    = False
            self._work_result  = ''
            self._work_dismiss = False
            self._dirty        = True

        threading.Thread(target=_move_then_teach, daemon=True).start()

    def _do_teach_save(self, slot: str, pos: int) -> str:
        # Cart is already at pos from live jogging — just persist
        self.hw.hw.set_slot_position(slot, pos)
        self.hw.hw.save()
        return f'{slot}={pos} saved'

    # ── Move X ────────────────────────────────────────────────

    def _enter_move_x(self):
        with self._lock:
            self._xmove_val    = self.hw.x_position
            self._xmove_step   = 1
            self._xmove_last_t = 0.0
            self._mode         = 'x_move'
            self._dirty        = True

    def _do_move_x(self, position: int):
        self._begin_work(f'Moving to {position}...',
                         self.hw.move_x, position)

    # ── Maintenance ────────────────────────────────────────────

    def _enter_maintenance(self):
        self._push(lambda: 'Maintenance', self._items_maintenance)

    def _items_maintenance(self) -> list:
        return [
            {'label': 'Home',        'hint': '  ',        'action': lambda: self._begin_work('Homing...', self._do_homing)},
            {'label': 'Teach Slots', 'hint': f'{ARROW} ', 'action': self._enter_teach_slots},
            {'label': 'Move X',      'hint': '  ',        'action': self._enter_move_x},
            {'label': 'Tare Scale',  'hint': '  ',        'action': lambda: self._begin_work('Taring...', self._do_tare_scale)},
            {'label': 'Read Weight', 'hint': '  ',        'action': lambda: self._begin_work('Reading...', self._do_read_weight)},
            {'label': 'E-Stop',      'hint': '  ',        'action': lambda: self._show_confirm('E-Stop', 'Stop all motors?', lambda: self._begin_work('Stopping...', self._do_emergency_stop))},
            {'label': 'Resume',      'hint': '  ',        'action': lambda: self._begin_work('Resuming...', self._do_resume)},
            {'label': 'Clean',       'hint': f'{ARROW} ', 'action': self._enter_clean},
            {'label': 'Hardware',    'hint': f'{ARROW} ', 'action': self._enter_hardware},
            {'label': 'Back',        'hint': '  ',        'action': self._pop},
        ]

    # ── Clean ──────────────────────────────────────────────────

    def _enter_clean(self):
        self._push(lambda: 'Clean', self._items_clean)

    def _items_clean(self) -> list:
        return [
            {'label': 'Mixer',  'hint': f'{ARROW} ', 'action': self._enter_clean_mixer},
            {'label': 'Spirit', 'hint': f'{ARROW} ', 'action': self._enter_clean_spirit},
            {'label': 'Back',   'hint': '  ',        'action': self._pop},
        ]

    def _enter_clean_mixer(self):
        def items_fn():
            lst = [
                {
                    'label':  slot.replace('_', ' '),
                    'hint':   f'{ARROW} ',
                    'action': lambda s=slot: self._enter_num(
                        'Clean Mixer',
                        f'Slot: {s[-1]}',
                        'Weight (g):',
                        1, 999, 1, 10,
                        lambda v, s=s: self._do_clean_mixer(s, v),
                    ),
                }
                for slot in MIXER_SLOTS
            ]
            lst.append({'label': 'Back', 'hint': '  ', 'action': self._pop})
            return lst
        self._push(lambda: 'Mixer', items_fn)

    def _enter_clean_spirit(self):
        def items_fn():
            lst = [
                {
                    'label':  slot.replace('_', ' '),
                    'hint':   f'{ARROW} ',
                    'action': lambda s=slot: self._enter_num(
                        'Clean Spirit',
                        f'Slot: {s[-1]}',
                        'Count:',
                        1, 99, 1, None,
                        lambda v, s=s: self._do_clean_spirit(s, v),
                    ),
                }
                for slot in SPIRIT_SLOTS
            ]
            lst.append({'label': 'Back', 'hint': '  ', 'action': self._pop})
            return lst
        self._push(lambda: 'Spirit', items_fn)

    # ── Hardware config ───────────────────────────────────────────

    def _hw_save_field(self, attr: str, value: int):
        """Update a HardwareConfig field and persist to disk."""
        hw = self.hw.hw
        if attr == 'pump_tubing_compensation_g':
            setattr(hw, attr, float(value))
        else:
            setattr(hw, attr, value)
        hw.save()
        print(f"[UI] hw.{attr} = {value}")

    def _hw_num(self, title: str, field_label: str, attr: str,
                min_v: int, max_v: int, step: int, fast_step: int):
        """Open num_entry for a HardwareConfig integer field."""
        current = getattr(self.hw.hw, attr, None)
        init = int(current) if current is not None else min_v
        self._enter_num(
            title, '', field_label,
            min_v, max_v, step, fast_step,
            lambda v: self._hw_save_field(attr, v),
            initial_v=init,
        )

    def _enter_hardware(self):
        self._push(lambda: 'Hardware', self._items_hardware)

    def _items_hardware(self) -> list:
        return [
            {'label': 'Optic',   'hint': f'{ARROW} ', 'action': self._enter_hw_optic},
            {'label': 'Motion',  'hint': f'{ARROW} ', 'action': self._enter_hw_motion},
            {'label': 'Scale',   'hint': f'{ARROW} ', 'action': self._enter_hw_scale},
            {'label': 'Back',    'hint': '  ',        'action': self._pop},
        ]

    def _enter_hw_optic(self):
        self._push(lambda: 'Optic', self._items_hw_optic)

    def _items_hw_optic(self) -> list:
        hw = self.hw.hw
        return [
            {'label': f'Pour angle  {hw.servo_pour_angle:>4}',
             'hint': '  ',
             'action': lambda: self._hw_num('Optic', 'Pour angle:', 'servo_pour_angle', 0, 180, 1, 5)},
            {'label': f'Close angle {hw.servo_close_angle:>4}',
             'hint': '  ',
             'action': lambda: self._hw_num('Optic', 'Close angle:', 'servo_close_angle', 0, 180, 1, 5)},
            {'label': f'Pour ms  {hw.pour_duration_ms:>6}',
             'hint': '  ',
             'action': lambda: self._hw_num('Optic', 'Pour ms:', 'pour_duration_ms', 100, 9000, 50, 200)},
            {'label': f'Settle ms{hw.settle_duration_ms:>6}',
             'hint': '  ',
             'action': lambda: self._hw_num('Optic', 'Settle ms:', 'settle_duration_ms', 100, 5000, 50, 200)},
            {'label': 'Back', 'hint': '  ', 'action': self._pop},
        ]

    def _enter_hw_motion(self):
        self._push(lambda: 'Motion', self._items_hw_motion)

    def _items_hw_motion(self) -> list:
        hw = self.hw.hw
        accel     = hw.x_accel     if hw.x_accel     is not None else 0
        max_speed = hw.x_max_speed if hw.x_max_speed is not None else 0
        return [
            {'label': f'Idle pos {hw.x_idle:>6}',
             'hint': '  ',
             'action': lambda: self._hw_num('Motion', 'Idle pos:', 'x_idle', 0, hw.x_max, 10, 100)},
            {'label': f'Accel   {accel:>7}',
             'hint': '  ',
             'action': lambda: self._hw_num('Motion', 'Accel:', 'x_accel', 0, 50000, 500, 2000)},
            {'label': f'MaxSpd  {max_speed:>7}',
             'hint': '  ',
             'action': lambda: self._hw_num('Motion', 'Max speed:', 'x_max_speed', 0, 20000, 100, 500)},
            {'label': 'Back', 'hint': '  ', 'action': self._pop},
        ]

    def _enter_hw_scale(self):
        self._push(lambda: 'Scale', self._items_hw_scale)

    def _items_hw_scale(self) -> list:
        hw  = self.hw.hw
        comp = int(round(hw.pump_tubing_compensation_g))
        return [
            {'label': f'Tubing comp {comp:>4}g',
             'hint': '  ',
             'action': lambda: self._hw_num('Scale', 'Tubing comp (g):', 'pump_tubing_compensation_g', 0, 50, 1, 5)},
            {'label': 'Calibrate Scale',
             'hint': '  ',
             'action': self._enter_calibrate_scale},
            {'label': 'Back', 'hint': '  ', 'action': self._pop},
        ]

    def _enter_calibrate_scale(self):
        """Guided calibration: enter weight → remove → tare → place → confirm → calibrate."""
        self._enter_num(
            'Calibrate Scale',
            'Known weight (g):',
            'Weight:',
            100, 5100, 100, 500,
            self._calib_step_remove,
            initial_v=100,
        )

    def _calib_step_remove(self, grams: int):
        """Step 1: ask user to remove everything from the scale."""
        self._show_confirm(
            'Calibrate Scale',
            'Remove all from scale',
            lambda g=grams: self._begin_work('Taring...', lambda: self._calib_do_tare(g)),
        )

    def _calib_do_tare(self, grams: int) -> str:
        """Tare the scale, then prompt user to place the known weight."""
        self.hw.tare_scale()
        # After tare completes, show the 'place weight' prompt from the main thread
        def _next():
            self._show_confirm(
                'Calibrate Scale',
                f'Place {grams}g on scale',
                lambda g=grams: self._begin_work('Calibrating...', lambda: self._do_calibrate_scale(g)),
            )
        threading.Thread(target=_next, daemon=True).start()
        return 'Scale tared'

    def _enter_num(self, title: str, subtitle: str, field: str,
                   min_v: int, max_v: int, step: int, fast_step,
                   cb, *, initial_v: int | None = None):
        """Enter the generic integer-entry mode.

        initial_v overrides the starting value (defaults to min_v).
        """
        start = max(min_v, min(max_v, initial_v)) if initial_v is not None else min_v
        with self._lock:
            self._nentry_title    = title
            self._nentry_subtitle = subtitle
            self._nentry_field    = field
            self._nentry_val      = start
            self._nentry_min      = min_v
            self._nentry_max      = max_v
            self._nentry_step     = step
            self._nentry_fast     = fast_step
            self._nentry_last_t   = 0.0
            self._nentry_cb       = cb
            self._mode            = 'num_entry'
            self._dirty           = True

    def _do_num_entry(self, cb, val: int):
        self._pop()   # leave slot list → back to Clean submenu
        cb(val)

    def _do_clean_mixer(self, slot: str, grams: int):
        self._begin_work(f'Cleaning {slot[-1]} {grams}g...',
                         self.hw.clean_mixer, slot, grams)

    def _do_clean_spirit(self, slot: str, count: int):
        self._begin_work(f'Cleaning {slot[-1]} x{count}...',
                         self.hw.clean_spirit, slot, count)

    # ── Recipes ────────────────────────────────────────────────

    def _enter_recipes(self):
        sp, mx = self._slug_maps()

        def disp(slug):
            return sp.get(slug) or mx.get(slug) or slug

        recipes = self.store.get_preset_recipes(self.attributes.attribute_slugs)
        if not recipes:
            self._show_info('Recipes', ['No recipes found.', 'Run Fetch first.', '', '[Press to return]'])
            return

        lines = []
        for sku, recipe in recipes.items():
            lines.append(f'{sku}:')
            for ing in recipe['ingredients']:
                name = disp(ing['name'])
                ml   = ing['ml']
                lines.append(f'  {name} {ml:.0f}ml')

        diy = self.store.get_diy_volumes(self.attributes.attribute_slugs)
        lines += [
            '-------------------',
            f"DIY Spirit:{diy.get('Spirit', '?'):.0f}ml",
            f"DIY Mixer: {diy.get('Mixer',  '?'):.0f}ml",
            '',
            '  [Press to return] ',
        ]
        self._show_info('Recipes', lines)

    # ── Slots ──────────────────────────────────────────────────

    def _enter_slots(self):
        self._push(lambda: 'Slots', self._items_slots)

    def _items_slots(self) -> list:
        items = []
        for slot in ALL_SLOTS:
            ing   = self.config.slot_ingredient(slot)
            name  = self._display(ing) if ing else '---'
            label = f'{slot}: {name}'
            items.append({
                'label':  label,
                'hint':   f'{ARROW} ',
                'action': lambda s=slot: self._enter_slot(s),
            })
        items.append({'label': 'Back', 'hint': '  ', 'action': self._pop})
        return items

    def _enter_slot(self, slot: str):
        def title_fn():
            return slot

        def items_fn():
            cur_ing  = self.config.slot_ingredient(slot)
            cur_name = self._display(cur_ing) if cur_ing else 'empty'
            lst = [{
                'label':  f'Set ({cur_name})',
                'hint':   f'{ARROW} ',
                'action': lambda: self._enter_ingredient(slot),
            }]
            if cur_ing:
                lst.append({
                    'label':  'Clear slot',
                    'hint':   '  ',
                    'action': lambda n=cur_name: self._show_confirm(
                        slot, f'Clear {n[:14]}?',
                        lambda: self._do_clear_slot(slot),
                    ),
                })
            lst.append({'label': 'Back', 'hint': '  ', 'action': self._pop})
            return lst

        self._push(title_fn, items_fn)

    def _enter_ingredient(self, slot: str):
        is_spirit = BarbotConfig.is_spirit_slot(slot)

        def items_fn():
            slugs = sorted(
                self.store.available_spirits if is_spirit
                else self.store.available_mixers
            )
            sp, mx = self._slug_maps()

            def disp(s):
                return sp.get(s) or mx.get(s) or s

            # Get already-assigned ingredients (excluding current slot)
            assigned = set()
            for s, ing in self.config.slot_mapping.items():
                if s != slot and ing:
                    assigned.add(ing)

            # Filter out already-assigned ingredients
            available_slugs = [s for s in slugs if s not in assigned]

            lst = [
                {
                    'label':  disp(s),
                    'hint':   '  ',
                    'action': lambda sl=s, nm=disp(s): self._do_set_slot(slot, sl, nm),
                }
                for s in available_slugs
            ]
            if not lst:
                lst.append({'label': '(none available)', 'hint': '  ', 'action': None})
            lst.append({'label': 'Cancel', 'hint': '  ', 'action': self._pop})
            return lst

        kind = 'Spirit' if is_spirit else 'Mixer'
        self._push(lambda: kind, items_fn)

    def _do_set_slot(self, slot: str, slug: str, name: str):
        self.config.set_slot(slot, slug)
        self.config.save()
        self._pop()   # leave ingredient screen
        self._pop()   # leave slot submenu → back to slots list
        self._begin_work(f'Sync {slot}...', self._sync_inventory)

    def _do_clear_slot(self, slot: str):
        self.config.clear_slot(slot)
        self.config.save()
        self._pop()   # back to slots list
        self._begin_work(f'Sync {slot}...', self._sync_inventory)

    def _sync_inventory(self) -> str:
        self.inventory.push_all()
        return 'Inventory synced!'

    # ── Viscosity ──────────────────────────────────────────────

    def _enter_viscosity(self):
        self._push(lambda: 'Viscosity', self._items_viscosity)

    def _items_viscosity(self) -> list:
        sp_visc = self._extract_viscosity('pa_spirits')
        mx_visc = self._extract_viscosity('pa_mixers')
        lst = []
        if sp_visc:
            lst.append({'label': 'Spirits', 'hint': f'{ARROW} ',
                        'action': lambda: self._enter_visc_list('Spirits', 'pa_spirits')})
        if mx_visc:
            lst.append({'label': 'Mixers',  'hint': f'{ARROW} ',
                        'action': lambda: self._enter_visc_list('Mixers', 'pa_mixers')})
        if not lst:
            lst.append({'label': '(no data – Fetch first)', 'hint': '  ', 'action': None})
        lst.append({'label': 'Back', 'hint': '  ', 'action': self._pop})
        return lst

    def _enter_visc_list(self, title: str, attr_slug: str):
        def items_fn():
            visc_map = self._extract_viscosity(attr_slug)
            lst = [
                {
                    'label':  f'{name} ({data["viscosity"]})',
                    'hint':   f'{ARROW} ',
                    'action': lambda n=name, d=dict(data): self._enter_visc_custom(n, d),
                }
                for name, data in visc_map.items()
            ]
            lst.append({'label': 'Back', 'hint': '  ', 'action': self._pop})
            return lst

        self._push(lambda: title, items_fn)

    def _enter_visc_custom(self, bottle: str, data: dict):
        """Enter dedicated viscosity-edit mode (not a menu push)."""
        with self._lock:
            self._vedit_bottle = bottle
            self._vedit_data   = data
            self._vedit_val    = data['viscosity']
            self._mode         = 'visc_edit'
            self._dirty        = True

    def _do_set_viscosity(self, bottle: str, data: dict, new_v: float):
        attr_slug = data['attr_slug']
        term_slug = data['term_slug']
        term_id   = data['term_id']
        attr_id   = self.attributes.attribute_ids.get(attr_slug)

        # Update local cache
        bp = self.attributes.bottle_properties.get(attr_slug, {})
        if term_slug in bp:
            bp[term_slug]['viscosity'] = new_v
        self.attributes.save()

        def _update() -> str:
            if attr_id and term_id:
                ok = self.woo.update_term_viscosity(attr_id, term_id, new_v)
                return f'{bottle[:10]}: {new_v}' if ok else f'{bottle[:8]} (local)'
            return f'{bottle[:8]} (local)'

        # Pop the bottle list, then show working dialog
        # (visc_edit is a mode, not on the stack — no stack pop needed for it)
        self._pop()   # leave bottle list

        self._begin_work(f'Saving {bottle[:12]}...', _update)

    # ── Run (order polling) ────────────────────────────────────

    def _enter_run(self):
        stop = threading.Event()
        with self._lock:
            self._run_stop    = stop
            self._run_count   = 0
            self._run_last_id = None
            self._mode        = 'run'
            self._dirty       = True
        # Turn off backlight during run mode
        self._lcd.backlight = False

        def _poll():
            while not stop.is_set():
                try:
                    self.woo.send_heartbeat()
                except Exception:
                    pass
                
                # Check if polling is paused and what action user selected
                with self._lock:
                    paused = self._polling_paused
                    retry = self._retry_drink

                if paused:
                    # Wait while paused, but keep checking for stop signal
                    stop.wait(1.0)
                    continue

                if retry:
                    # User selected RETRY — retry only the mixer part (spirits already done)
                    log_info("POLL", "Retrying mixer dispensing per user selection...")
                    with self._lock:
                        self._retry_drink = False
                    # Fetch the last order and retry just the mixers
                    pending = self.woo.fetch_all('orders', {'status': 'processing'})
                    for order in sorted(pending, key=lambda o: o['id']):
                        if order['id'] == self._run_last_id:
                            try:
                                self.orders.process_order(order, retry_mixers_only=True)
                                with self._lock:
                                    self._run_count += 1
                                log_info("POLL", f"Mixer retry succeeded for order {order['id']}")
                            except Exception as e:
                                log_error("POLL", f"Mixer retry failed: {e}")
                                self.pause_polling(str(e)[:20] or "Retry failed")
                            break
                    continue
                
                try:
                    pending = self.woo.fetch_all('orders', {'status': 'processing'})
                    for order in sorted(pending, key=lambda o: o['id']):
                        if stop.is_set():
                            break
                        try:
                            self.orders.process_order(order)
                            with self._lock:
                                self._run_count  += 1
                                self._run_last_id = order.get('id')
                        except Exception as e:
                            log_error("POLL", f"Error processing order {order.get('id')}: {e}")
                            self.pause_polling(str(e)[:20] or f"Order {order.get('id')} Failed")
                            break
                except Exception:
                    pass
                stop.wait(self.config.poll_interval)

        threading.Thread(target=_poll, daemon=True).start()

    def _do_stop_run(self):
        """Called when YES is confirmed on the stop dialog."""
        with self._lock:
            if self._run_stop:
                self._run_stop.set()
            # Re-engage PIN lock only if enabled
            if ENABLE_PIN_LOCK:
                self._engage_pin_lock_locked(reset_to_root=True)
        self._lcd.backlight = True

    # ── Quit ───────────────────────────────────────────────────

    def _do_quit(self):
        with self._lock:
            self._alive = False
