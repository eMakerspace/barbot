#!/usr/bin/env python3
"""
Barbot LCD View
───────────────
Pure view layer: drives the 4×20 I²C LCD and rotary encoder.
Knows nothing about hardware, orders, WooCommerce, or config.

The FSM is the sole orchestrator:
  • It calls show_*() methods to update what is displayed.
  • It registers callbacks (on_rotate, on_press) to receive input events.

Hardware:
  LCD  : 2004A via I2C PCF8574 backpack @ 0x3F
  CLK  → BCM 27 (BOARD 13)
  DT   → BCM 17 (BOARD 11)
  SW   → BCM 22 (BOARD 15)

Controls:
  Rotate → navigate / adjust
  Press  → confirm / dismiss
"""

import logging
import threading
import time
from typing import Callable

import RPi.GPIO as GPIO

from lcd import LcdDisplay, COLS, ROWS
from encoder import RotaryEncoder

log = logging.getLogger("LCD")

GPIO_CLK = 27
GPIO_DT  = 17
GPIO_SW  = 22

VISIBLE = 3          # scrollable item rows below the header
SPINNER = ('|', '/', '-', '\\')
ARROW   = '>'
LABEL_W = 16
HINT_W  = 2


class LCDMenu:
    """
    Pure view / input layer.  All state that drives rendering is set by
    the FSM through the public show_*() API.  Input events are forwarded
    to FSM-provided callbacks via register_input_handler().
    """

    def __init__(self):
        self._lcd     = LcdDisplay()
        self._encoder = RotaryEncoder(
            GPIO_CLK, GPIO_DT, GPIO_SW,
            on_rotate=self._on_rotate,
            on_press=self._on_press,
        )

        self._lock     = threading.Lock()
        self._alive    = True
        self._spin_idx = 0

        # ── Render mode ──────────────────────────────────────────────────────
        # 'menu' | 'info' | 'working' | 'confirm' | 'run' | 'mixing' |
        # 'visc_edit' | 'x_move' | 'num_entry'
        self._mode  = 'menu'
        self._dirty = True

        # ── Menu mode ────────────────────────────────────────────────────────
        # Items: list of {'label': str, 'hint': str}  (no actions – FSM owns those)
        self._menu_title:  str        = ''
        self._menu_items:  list[dict] = []
        self._menu_cursor: int        = 0
        self._menu_scroll: int        = 0

        # ── Info mode ────────────────────────────────────────────────────────
        self._info_title:  str        = ''
        self._info_lines:  list[str]  = []
        self._info_scroll: int        = 0

        # ── Working mode ─────────────────────────────────────────────────────
        self._work_title:   str  = ''
        self._work_done:    bool = False
        self._work_result:  str  = ''
        self._work_dismiss: bool = False

        # ── Confirm mode ─────────────────────────────────────────────────────
        self._conf_title: str  = ''
        self._conf_msg:   str  = ''
        self._conf_yn:    int  = 0   # 0 = YES, 1 = NO
        self._conf_cb:    Callable | None = None
        self._conf_back_mode: str = 'menu'

        # ── Run mode ─────────────────────────────────────────────────────────
        self._run_count:           int   = 0
        self._run_last_id:         int | None = None
        self._run_backlight_until: float = 0.0

        # ── Mixing mode ──────────────────────────────────────────────────────
        self._mix_drink_num: int = 0
        self._mix_total:     int = 0
        self._mix_name:      str = ''

        # ── Visc-edit mode ───────────────────────────────────────────────────
        self._vedit_bottle:    str   = ''
        self._vedit_val:       float = 1.0
        self._vedit_min:       float = 0.1
        self._vedit_max:       float = 10.0
        self._vedit_step:      float = 0.1

        # ── X-move mode ──────────────────────────────────────────────────────
        self._xmove_current:   int   = 0
        self._xmove_target:    int   = 0
        self._xmove_x_max:     int   = 6000
        self._xmove_step:      int   = 1
        self._xmove_last_t:    float = 0.0

        # ── Num-entry mode ───────────────────────────────────────────────────
        self._nentry_title:    str        = ''
        self._nentry_subtitle: str        = ''
        self._nentry_field:    str        = ''
        self._nentry_val:      int        = 0
        self._nentry_min:      int        = 0
        self._nentry_max:      int        = 100
        self._nentry_step:     int        = 1
        self._nentry_fast:     int | None = None
        self._nentry_last_t:   float      = 0.0

        # ── FSM input callbacks ───────────────────────────────────────────────
        # The FSM registers these so it can react to knob/button events.
        self._on_menu_select:   Callable[[int], None] | None = None   # index selected
        self._on_rotate_cb:     Callable[[int], None] | None = None   # d = +1 / -1 (non-menu)
        self._on_press_cb:      Callable[[], None]    | None = None   # generic press

        # ── Marquee ──────────────────────────────────────────────────────────
        self._marq_label:  str   = ''
        self._marq_offset: int   = 0
        self._marq_dir:    int   = 1
        self._marq_next:   float = 0.0
        self._marq_pause:  float = 0.0

    # ═════════════════════════════════════════════════════════════════════════
    # Lifecycle
    # ═════════════════════════════════════════════════════════════════════════

    def start(self):
        """Initialise GPIO and start the encoder polling thread."""
        GPIO.setmode(GPIO.BCM)
        self._lcd.clear()
        self._encoder.start()
        log.info("[LCD] View started")

    def stop(self):
        """Release GPIO and blank the display."""
        self._encoder.stop()
        self._lcd.clear()
        self._write_row(1, '      Goodbye!      ')
        time.sleep(1.0)
        self._lcd.clear()
        self._lcd.backlight = False
        GPIO.cleanup()
        log.info("[LCD] View stopped")

    def render_loop(self):
        """
        Blocking render loop – call from the FSM's main thread (or a
        dedicated thread) after start().  Exits when alive is set False.
        """
        try:
            while self._alive:
                self._render()
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass

    def shutdown(self):
        """Signal the render loop to exit."""
        with self._lock:
            self._alive = False

    # ═════════════════════════════════════════════════════════════════════════
    # FSM input-callback registration
    # ═════════════════════════════════════════════════════════════════════════

    def register_menu_select(self, cb: Callable[[int], None]):
        """FSM registers this to receive the index of the item the user pressed."""
        self._on_menu_select = cb

    def register_rotate(self, cb: Callable[[int], None]):
        """FSM registers this for non-menu rotation events (visc edit, x-move, etc.)."""
        self._on_rotate_cb = cb

    def register_press(self, cb: Callable[[str], None]):
        """
        FSM registers this for press events.
        The callback receives the current ui mode string so the FSM can
        construct a PressEvent(ui_mode) without knowing LCD internals.
        """
        self._on_press_cb = cb

    # ═════════════════════════════════════════════════════════════════════════
    # Public show_* API  (called by FSM to push display state)
    # ═════════════════════════════════════════════════════════════════════════

    def show_menu(self, title: str, items: list[dict]):
        """
        Display a scrollable menu.
        items: [{'label': str, 'hint': str (2 chars)}]
        Selection is reported via the on_menu_select callback.
        """
        with self._lock:
            self._menu_title  = title
            self._menu_items  = items
            self._menu_cursor = 0
            self._menu_scroll = 0
            self._mode        = 'menu'
            self._dirty       = True
        self._marq_reset()
        log.info("[LCD] show_menu('%s', %d items)", title, len(items))

    def show_info(self, title: str, lines: list[str]):
        """Display a scrollable info screen. Press to dismiss (fires on_press_cb)."""
        with self._lock:
            self._info_title  = title
            self._info_lines  = [str(l)[:COLS] for l in lines]
            self._info_scroll = 0
            self._mode        = 'info'
            self._dirty       = True
        log.info("[LCD] show_info('%s', %d lines)", title, len(lines))

    def show_working(self, title: str):
        """Show the 'Working…' spinner screen."""
        with self._lock:
            self._work_title   = title
            self._work_done    = False
            self._work_result  = ''
            self._work_dismiss = False
            self._mode         = 'working'
            self._dirty        = True
        log.info("[LCD] show_working('%s')", title)

    def set_work_done(self, result: str = 'Done!'):
        """Transition working screen to 'Complete!' with result text."""
        with self._lock:
            self._work_result = result[:18]
            self._work_done   = True
            self._dirty       = True
        log.info("[LCD] set_work_done('%s')", result)

    def show_confirm(self, title: str, msg: str, on_yes: Callable, back_mode: str = 'menu'):
        """
        Show YES/NO confirm dialog.
        on_yes is called if the user confirms; back_mode is the mode restored on NO.
        """
        with self._lock:
            self._conf_title     = title
            self._conf_msg       = msg[:COLS]
            self._conf_yn        = 0
            self._conf_cb        = on_yes
            self._conf_back_mode = back_mode
            self._mode           = 'confirm'
            self._dirty          = True
        log.info("[LCD] show_confirm('%s', '%s')", title, msg)

    def show_run(self, count: int, last_id: int | None):
        """Switch to run/polling display and update counters."""
        with self._lock:
            self._run_count   = count
            self._run_last_id = last_id
            self._mode        = 'run'
            self._dirty       = True
        self._lcd.backlight = False

    def update_run(self, count: int, last_id: int | None):
        """Update run counters without changing mode."""
        with self._lock:
            self._run_count   = count
            self._run_last_id = last_id
            self._dirty       = True

    def show_mixing(self, drink_num: int, total: int, name: str):
        """Show the mixing progress screen."""
        with self._lock:
            self._mix_drink_num = drink_num
            self._mix_total     = total
            self._mix_name      = name
            self._mode          = 'mixing'
            self._dirty         = True
        self._marq_reset()
        self._lcd.backlight = True
        log.info("[LCD] show_mixing(%d/%d '%s')", drink_num, total, name)

    def clear_mixing(self):
        """Return from mixing back to run mode."""
        with self._lock:
            self._mode  = 'run'
            self._dirty = True
        self._lcd.backlight = False
        log.info("[LCD] clear_mixing")

    def show_visc_edit(self, bottle: str, val: float,
                       min_v: float = 0.1, max_v: float = 10.0, step: float = 0.1):
        """Show viscosity adjustment screen. Rotation changes val; press fires on_press_cb."""
        with self._lock:
            self._vedit_bottle = bottle
            self._vedit_val    = val
            self._vedit_min    = min_v
            self._vedit_max    = max_v
            self._vedit_step   = step
            self._mode         = 'visc_edit'
            self._dirty        = True
        log.info("[LCD] show_visc_edit('%s', %.1f)", bottle, val)

    def show_x_move(self, current: int, x_max: int):
        """Show the manual X-move screen. Rotation adjusts target; press fires on_press_cb."""
        with self._lock:
            self._xmove_current = current
            self._xmove_target  = current
            self._xmove_x_max   = x_max
            self._xmove_step    = 1
            self._xmove_last_t  = 0.0
            self._mode          = 'x_move'
            self._dirty         = True
        log.info("[LCD] show_x_move(current=%d, x_max=%d)", current, x_max)

    def show_num_entry(self, title: str, subtitle: str, field: str,
                       min_v: int, max_v: int, step: int, fast_step: int | None):
        """Show a generic integer-entry screen. Press fires on_press_cb with current value."""
        with self._lock:
            self._nentry_title    = title
            self._nentry_subtitle = subtitle
            self._nentry_field    = field
            self._nentry_val      = min_v
            self._nentry_min      = min_v
            self._nentry_max      = max_v
            self._nentry_step     = step
            self._nentry_fast     = fast_step
            self._nentry_last_t   = 0.0
            self._mode            = 'num_entry'
            self._dirty           = True
        log.info("[LCD] show_num_entry('%s')", title)

    # ── Accessors for FSM to read back current input-mode values ─────────────

    @property
    def visc_val(self) -> float:
        return self._vedit_val

    @property
    def xmove_target(self) -> int:
        return self._xmove_target

    @property
    def nentry_val(self) -> int:
        return self._nentry_val

    @property
    def current_mode(self) -> str:
        return self._mode

    # ═════════════════════════════════════════════════════════════════════════
    # Input handlers  (called from RotaryEncoder HAL thread)
    # ═════════════════════════════════════════════════════════════════════════

    def _on_rotate(self, d: int):
        with self._lock:
            m = self._mode

            if m == 'run':
                self._run_backlight_until = time.time() + 5.0
                self._lcd.backlight = True

            elif m == 'visc_edit':
                self._vedit_val = round(
                    max(self._vedit_min,
                        min(self._vedit_max, self._vedit_val + d * self._vedit_step)),
                    1,
                )
                self._dirty = True

            elif m == 'x_move':
                now = time.time()
                dt  = now - self._xmove_last_t
                self._xmove_last_t = now
                step = 100 if dt < 0.01 else 1
                self._xmove_step   = step
                self._xmove_target = max(0, min(self._xmove_x_max,
                                                self._xmove_target + d * step))
                self._dirty = True

            elif m == 'num_entry':
                now = time.time()
                dt  = now - self._nentry_last_t
                self._nentry_last_t = now
                s = (self._nentry_fast if dt < 0.05 and self._nentry_fast
                     else self._nentry_step)
                self._nentry_val = max(self._nentry_min,
                                       min(self._nentry_max, self._nentry_val + d * s))
                self._dirty = True

            elif m == 'menu' and self._menu_items:
                old = self._menu_cursor
                self._menu_cursor = max(0, min(len(self._menu_items) - 1,
                                               self._menu_cursor + d))
                if self._menu_cursor != old:
                    self._marq_label = ''
                if self._menu_cursor < self._menu_scroll:
                    self._menu_scroll = self._menu_cursor
                elif self._menu_cursor >= self._menu_scroll + VISIBLE:
                    self._menu_scroll = self._menu_cursor - VISIBLE + 1
                self._dirty = True

            elif m == 'info':
                max_s = max(0, len(self._info_lines) - (ROWS - 1))
                self._info_scroll = max(0, min(max_s, self._info_scroll + d))
                self._dirty = True

            elif m == 'confirm':
                self._conf_yn = 1 - self._conf_yn
                self._dirty   = True

        # Forward to FSM for non-menu rotations (e.g. visc_edit value already
        # updated above; FSM may want to know about all rotations for other modes)
        if self._on_rotate_cb and m not in ('menu', 'info', 'confirm',
                                            'visc_edit', 'x_move', 'num_entry'):
            self._on_rotate_cb(d)

    def _on_press(self):
        action = None

        with self._lock:
            m = self._mode

            if m == 'working':
                if self._work_done:
                    self._mode  = 'menu'
                    self._dirty = True

            elif m == 'info':
                # Transition display back to menu, then notify FSM with the mode
                # that was active so it can react to the info dismissal.
                self._mode  = 'menu'
                self._dirty = True
                if self._on_press_cb:
                    action = lambda: self._on_press_cb('info')

            elif m == 'confirm':
                yn          = self._conf_yn
                cb          = self._conf_cb
                back        = self._conf_back_mode
                self._mode  = 'menu' if yn == 0 else back
                self._dirty = True
                if yn == 0:
                    action = cb

            elif m in ('run', 'visc_edit', 'x_move', 'num_entry'):
                if m == 'run':
                    self._lcd.backlight = True
                if self._on_press_cb:
                    action = lambda mode=m: self._on_press_cb(mode)

            elif m == 'menu' and self._menu_items:
                idx    = self._menu_cursor
                sel_cb = self._on_menu_select
                action = (lambda i=idx, c=sel_cb: c(i)) if sel_cb else None

        if action:
            action()

    # ═════════════════════════════════════════════════════════════════════════
    # Render dispatcher
    # ═════════════════════════════════════════════════════════════════════════

    def _render(self):
        with self._lock:
            mode  = self._mode
            dirty = self._dirty
            if mode not in ('working', 'run', 'mixing', 'menu') and not dirty:
                return
            self._dirty = False

        if   mode == 'menu':      self._draw_menu()
        elif mode == 'info':      self._draw_info()
        elif mode == 'working':   self._draw_working()
        elif mode == 'confirm':   self._draw_confirm()
        elif mode == 'run':       self._draw_run()
        elif mode == 'mixing':    self._draw_mixing()
        elif mode == 'visc_edit': self._draw_visc_edit()
        elif mode == 'x_move':    self._draw_x_move()
        elif mode == 'num_entry': self._draw_num_entry()

    # ── Draw helpers ─────────────────────────────────────────────────────────

    def _write_row(self, r: int, text: str):
        self._lcd.write_row(r, text)

    @staticmethod
    def _hdr(title: str, up: bool = False, dn: bool = False) -> str:
        u = '^' if up else '='
        d = 'v' if dn else '='
        return f'={u}={title[:14]:^14}={d}='

    def _draw_menu(self):
        items  = self._menu_items
        cur    = self._menu_cursor
        scr    = self._menu_scroll
        n      = len(items)
        self._write_row(0, self._hdr(self._menu_title, scr > 0, scr + VISIBLE < n))
        for row in range(VISIBLE):
            idx = scr + row
            if idx >= n:
                self._write_row(row + 1, '')
                continue
            item  = items[idx]
            sel   = ARROW if idx == cur else ' '
            hint  = f'{item.get("hint", "  "):<2}'[:2]
            label = item.get('label', '')
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
            self._write_row(r, lines[li] if li < n else '')

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
            self._write_row(2, '')
            self._write_row(3, '')
        else:
            self._write_row(0, '==== Complete! =====')
            self._write_row(1, f' {result[:18]:<18}')
            self._write_row(2, '')
            self._write_row(3, '  [Press to return] ')
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

    def _draw_run(self):
        with self._lock:
            count    = self._run_count
            last_id  = self._run_last_id
            bl_until = self._run_backlight_until
        if time.time() >= bl_until:
            self._lcd.backlight = False
        self._spin_idx = (self._spin_idx + 1) % len(SPINNER)
        spin     = SPINNER[self._spin_idx]
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
        vis  = self._marq_tick(name, 17)
        self._write_row(0, self._hdr(f'Mixing {num}/{total}'))
        self._write_row(1, f' {spin} {vis}')
        self._write_row(2, '')
        self._write_row(3, '')

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
            cur    = self._xmove_current
            target = self._xmove_target
            step   = self._xmove_step
        self._write_row(0, self._hdr('Move X'))
        self._write_row(1, f'  Current:  {cur:>5}     ')
        self._write_row(2, f'  Target:   {target:>5}     ')
        self._write_row(3, f'  Step:{step:>4}  [Press] ')

    def _draw_num_entry(self):
        with self._lock:
            title    = self._nentry_title
            subtitle = self._nentry_subtitle
            field    = self._nentry_field
            val      = self._nentry_val
        self._write_row(0, self._hdr(title))
        self._write_row(1, f'  {subtitle:<18}')
        self._write_row(2, f'  {field:<10}{val:>6}  ')
        self._write_row(3, '  [Press to confirm]')

    # ═════════════════════════════════════════════════════════════════════════
    # Marquee (ping-pong scroll for the selected menu row)
    # ═════════════════════════════════════════════════════════════════════════

    def _marq_reset(self):
        self._marq_label  = ''
        self._marq_offset = 0
        self._marq_dir    = 1
        self._marq_next   = time.time() + 0.4
        self._marq_pause  = time.time() + 1.2

    def _marq_tick(self, label: str, width: int) -> str:
        if len(label) <= width:
            return f'{label:<{width}}'
        if label != self._marq_label:
            self._marq_label  = label
            self._marq_offset = 0
            self._marq_dir    = 1
            self._marq_pause  = 0.0
            self._marq_next   = time.time() + 0.4
        now = time.time()
        if now >= self._marq_next:
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
            with self._lock:
                self._dirty = True
        return label[self._marq_offset: self._marq_offset + width]
