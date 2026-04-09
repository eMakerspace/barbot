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

import RPi.GPIO as GPIO

from config import BarbotConfig, AttributesConfig, SPIRIT_SLOTS, MIXER_SLOTS, ALL_SLOTS
from store import StoreConfig
from woo_client import WooClient
from hardware import HardwareInterface, X_MOVE_MIN, X_MOVE_MAX
from inventory import InventoryManager
from orders import OrderProcessor
from hal_lcd import LcdDisplay, COLS, ROWS
from hal_encoder import RotaryEncoder

# ── GPIO pin assignments ──────────────────────────────────────
GPIO_CLK = 27   # BCM (BOARD 13)
GPIO_DT  = 17   # BCM (BOARD 11)
GPIO_SW  = 22   # BCM (BOARD 15)

VISIBLE  = 3    # item rows; row 0 is always the header

SPINNER  = ('|', '/', '-', '\\')
ARROW    = '\x7e'   # → glyph in HD44780 ROM A02

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
        # 'menu' | 'info' | 'working' | 'confirm' | 'run' | 'visc_edit' | 'x_move' | 'num_entry'
        self._mode = 'menu'

        # visc_edit state
        self._vedit_bottle = ''
        self._vedit_data:  dict = {}
        self._vedit_val    = 1.0

        # x_move state
        self._xmove_val    = 0       # current target position
        self._xmove_step   = 1       # last computed acceleration step
        self._xmove_last_t = 0.0     # timestamp of last rotation event

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
        GPIO.setmode(GPIO.BCM)

        self._lcd.clear()
        self._encoder.start()

        # Startup sequence (blocking)
        self._begin_work('Homing...', self._do_homing)
        self._wait_work()

        self._normalize_slots()

        self._begin_work('Fetching...', self._do_fetch)
        self._wait_work()

        # Main menu
        self._push(self._title_main, self._items_main)

        try:
            while self._alive:
                self._render()
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            try:
                self._lcd.clear()
                self._write_row(0, 'Error!')
                self._write_row(1, str(e)[:20])
                time.sleep(2.0)
            except Exception:
                pass
        finally:
            self._encoder.stop()
            self._lcd.clear()
            self._write_row(1, '      Goodbye!      ')
            time.sleep(1.0)
            self._lcd.clear()
            self._lcd.backlight = False
            GPIO.cleanup()

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
            m = self._mode

            if m == 'run':
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
                self._xmove_val  = max(X_MOVE_MIN,
                                   min(X_MOVE_MAX, self._xmove_val + d * step))
                self._dirty = True

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

            elif m == 'confirm':
                self._conf_yn = 1 - self._conf_yn
                self._dirty = True

    def _on_press(self):
        """Handle button press. Action is called OUTSIDE the lock."""
        action = None

        with self._lock:
            m = self._mode

            if m == 'working':
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

    # ══════════════════════════════════════════════════════════
    # Render dispatcher
    # ══════════════════════════════════════════════════════════

    def _render(self):
        with self._lock:
            mode  = self._mode
            dirty = self._dirty
            # 'working', 'run', and 'menu' always redraw
            # (menu needs it for marquee; row cache prevents redundant I2C writes)
            if mode not in ('working', 'run', 'menu') and not dirty:
                return
            self._dirty = False

        if   mode == 'menu':    self._draw_menu()
        elif mode == 'info':    self._draw_info()
        elif mode == 'working':   self._draw_working()
        elif mode == 'confirm':   self._draw_confirm()
        elif mode == 'run':       self._draw_run()
        elif mode == 'visc_edit':  self._draw_visc_edit()
        elif mode == 'x_move':    self._draw_x_move()
        elif mode == 'num_entry': self._draw_num_entry()

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

    # ══════════════════════════════════════════════════════════
    # Startup tasks
    # ══════════════════════════════════════════════════════════

    def _do_homing(self) -> str:
        self.hw.homing()
        return 'Homing complete!'

    def _do_fetch(self) -> str:
        self._fetch_attributes()
        self.store.fetch(self.woo, self.attributes.term_slugs)
        return 'Store loaded!'

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
            {'label': 'Move X', 'hint': '  ',        'action': self._enter_move_x},
            {'label': 'Clean',  'hint': f'{ARROW} ', 'action': self._enter_clean},
            {'label': 'Back',   'hint': '  ',        'action': self._pop},
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

    def _enter_num(self, title: str, subtitle: str, field: str,
                   min_v: int, max_v: int, step: int, fast_step,
                   cb):
        """Enter the generic integer-entry mode."""
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
                try:
                    pending = self.woo.fetch_all('orders', {'status': 'processing'})
                    for order in pending:
                        if stop.is_set():
                            break
                        self.orders.process_order(order)
                        with self._lock:
                            self._run_count  += 1
                            self._run_last_id = order.get('id')
                except Exception:
                    pass
                stop.wait(self.config.poll_interval)

        threading.Thread(target=_poll, daemon=True).start()

    def _do_stop_run(self):
        """Called when YES is confirmed on the stop dialog."""
        with self._lock:
            if self._run_stop:
                self._run_stop.set()
            self._mode  = 'menu'
            self._dirty = True
        self._lcd.backlight = True

    # ── Quit ───────────────────────────────────────────────────

    def _do_quit(self):
        with self._lock:
            self._alive = False
