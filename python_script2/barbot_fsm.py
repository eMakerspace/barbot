"""
Barbot Main FSM
===============
Sole orchestrator.  Dependency graph:

  BarbotFSM
    ├── AbstractLED          (LED firmware interface)
    ├── AbstractMachine      (motion + dispense + scale firmware interface)
    ├── BarbotRepository     (single data-access layer)
    ├── InventoryManager     (stock sync – uses BarbotRepository)
    ├── DrinkResolver        (recipe resolution – uses BarbotRepository)
    └── LCDMenu (ui)         (pure view – driven via show_*() calls)

The FSM owns:
  - All startup / menu / settings / maintenance / run / pour / cleanup states
  - A thread-safe Queue for UI input events so the encoder ISR never blocks
    the FSM tick loop
  - CupCheckThread that monitors the cup sensor during the Emergency Zone

The FSM does NOT own:
  - WooClient, StoreConfig, AttributesConfig, BarbotConfig  (all via repo)
  - Scale (encapsulated inside AbstractMachine)
  - LCD rendering (all via ui.show_*())
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from config import SPIRIT_SLOTS, MIXER_SLOTS, ALL_SLOTS
from interfaces import AbstractLED, AbstractMachine
from hardware_dummy import MixerStall
from repository import BarbotRepository
from inventory import InventoryManager
from mixer import DrinkSpec, DrinkResolver
from orders import HeartbeatService

log = logging.getLogger("FSM")

ARROW = '>'


# ─────────────────────────────────────────────────────────────────────────────
# UI event types posted onto the FSM queue by LCDMenu
# ─────────────────────────────────────────────────────────────────────────────

class UIEvent:
    pass

@dataclass
class MenuSelectEvent(UIEvent):
    index: int

@dataclass
class PressEvent(UIEvent):
    ui_mode: str   # the lcd mode at time of press


# ─────────────────────────────────────────────────────────────────────────────
# State & Mode enumerations
# ─────────────────────────────────────────────────────────────────────────────

class State(Enum):
    HOMING                     = auto()
    FETCH_STORE_DATA           = auto()
    MAIN_MENU                  = auto()
    SETTINGS                   = auto()
    SET_VISCOSITY_LIST         = auto()
    SET_VISCOSITY_EDIT         = auto()
    SET_DRINK_SLOTS            = auto()
    SET_SLOT_INGREDIENT        = auto()
    MAINTENANCE                = auto()
    MOVE_X                     = auto()
    CALIBRATE_SCALE            = auto()
    READ_WEIGHT                = auto()
    CLEAN                      = auto()
    CLEAN_SELECT_SPIRIT_SLOT   = auto()
    CLEAN_ENTER_SPIRIT_COUNT   = auto()
    CLEAN_SELECT_MIXER_SLOT    = auto()
    CLEAN_ENTER_MIXER_GRAMS    = auto()
    VERIFY_CUP_MOUNTED         = auto()
    START_CUP_THREAD_CLEAN     = auto()
    RUN_IDLE                   = auto()
    WAIT_FOR_CUP_ADDITION      = auto()
    CHECK_ORDER                = auto()
    MOVE_TO_POS                = auto()
    POUR_SPIRIT                = auto()
    POUR_MIXER                 = auto()
    CHECK_NEXT_POSITION        = auto()
    MOVE_TO_IDLE               = auto()
    MIXER_ERROR                = auto()
    VERIFY_CUP_BEFORE_RESUME   = auto()
    STOP                       = auto()
    WAIT_FOR_CUP_ADDED         = auto()
    DISARM_EMERGENCY_STOP      = auto()
    EMERGENCY_RECOVERY_ROUTING = auto()
    REPEAT_CURRENT_DRINK       = auto()
    WAIT_FOR_CUP_REMOVAL       = auto()
    AFTER_CUP_REMOVAL_ROUTING  = auto()
    WAIT_FOR_NEW_CUP_ADDED     = auto()
    CHECK_FOR_OTHER_DRINKS     = auto()
    CLEANUP                    = auto()
    EXIT                       = auto()


class Mode(Enum):
    RUN   = "run"
    CLEAN = "clean"


# ─────────────────────────────────────────────────────────────────────────────
# Dispense step
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DispenseStep:
    kind:       str
    slot:       str
    ingredient: str
    pours:      int   = 0
    viscosity:  float = 1.0
    grams:      float = 0.0

    def label(self) -> str:
        if self.kind == "spirit":
            return (f"spirit slot={self.slot} ingredient={self.ingredient} "
                    f"pours={self.pours} viscosity={self.viscosity:.2f}")
        return f"mixer slot={self.slot} ingredient={self.ingredient} grams={self.grams:.1f}"


def steps_from_spec(spec: DrinkSpec) -> list[DispenseStep]:
    steps: list[DispenseStep] = []
    for s in spec.spirits:
        if s["slot"] is None:
            log.warning("[FSM] No slot for spirit '%s' – skipping", s["ingredient"])
            continue
        steps.append(DispenseStep(
            kind="spirit", slot=s["slot"], ingredient=s["ingredient"],
            pours=s["pours"], viscosity=s.get("viscosity", 1.0),
        ))
    for m in spec.mixers:
        if m["slot"] is None:
            log.warning("[FSM] No slot for mixer '%s' – skipping", m["ingredient"])
            continue
        steps.append(DispenseStep(
            kind="mixer", slot=m["slot"], ingredient=m["ingredient"],
            grams=m["ml"],
        ))
    return steps


# ─────────────────────────────────────────────────────────────────────────────
# Cup-check background thread
# ─────────────────────────────────────────────────────────────────────────────

class CupCheckThread:
    """
    Background thread that monitors cup presence during dispensing.
    Thread-safe: calls AbstractMachine.cup_present() which must be thread-safe
    (guarded by its own lock). Signals emergency stop via threading.Event if cup
    is removed while active.
    """
    def __init__(self, machine: AbstractMachine, emergency_stop: threading.Event):
        self._machine        = machine
        self._emergency_stop = emergency_stop
        self._active         = threading.Event()
        self._stop           = threading.Event()
        self._thread         = threading.Thread(
            target=self._run, daemon=True, name="CupCheckThread")

    def start(self):
        log.info("[CUP_THREAD] Starting")
        self._thread.start()

    def activate(self):
        log.info("[CUP_THREAD] ACTIVE")
        self._active.set()

    def deactivate(self):
        log.info("[CUP_THREAD] INACTIVE")
        self._active.clear()

    def stop(self):
        log.info("[CUP_THREAD] Stopping")
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            if self._active.is_set() and not self._machine.cup_present():
                if not self._emergency_stop.is_set():
                    log.warning("[CUP_THREAD] !! Cup removed – EMERGENCY STOP !!")
                    self._emergency_stop.set()
            time.sleep(0.05)


# ─────────────────────────────────────────────────────────────────────────────
# Main FSM
# ─────────────────────────────────────────────────────────────────────────────

class BarbotFSM:

    def __init__(
        self,
        led:       AbstractLED,
        machine:   AbstractMachine,
        repo:      BarbotRepository,
        inventory: InventoryManager,
        resolver:  DrinkResolver,
        ui=None,
    ):
        self.led       = led
        self.machine   = machine
        self.repo      = repo
        self.inventory = inventory
        self.resolver  = resolver
        self.ui        = ui   # LCDMenu – injected after construction

        self._state = State.HOMING
        self._mode  = Mode.RUN

        # Thread-safe event queue: UI posts events here; FSM reads them
        self._ui_events: queue.Queue[UIEvent] = queue.Queue()

        # Slot / viscosity editing temporaries
        self._edit_slot:         str              = ''
        self._edit_visc_data:    dict             = {}
        self._edit_visc_bottle:  str              = ''
        self._visc_attr_entries: list[tuple[str, str]] = []
        self._slot_edit_meta:    dict             = {}

        # Clean temporaries
        self._clean_slot:  str                = ''
        self._clean_steps: list[DispenseStep] = []

        # Order / drink tracking
        self._order_specs:  list[DrinkSpec]    = []
        self._drink_index:  int                = 0
        self._order_id:     Optional[int]      = None
        self._run_count:    int                = 0

        # Dispense plan
        self._steps:        list[DispenseStep]     = []
        self._step_index:   int                    = 0
        self._current_step: Optional[DispenseStep] = None

        # Emergency stop
        self._emergency_stop = threading.Event()
        self._cup_thread     = CupCheckThread(machine, self._emergency_stop)

        # Heartbeat service
        self._heartbeat = HeartbeatService(repo)

        log.info("[FSM] BarbotFSM initialised")

    # ─────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────────────

    def run(self):
        self._cup_thread.start()
        self._heartbeat.start()

        if self.ui:
            self.ui.start()
            # Callbacks post events onto the queue – never block the encoder ISR
            self.ui.register_menu_select(
                lambda idx: self._ui_events.put(MenuSelectEvent(idx)))
            self.ui.register_press(
                lambda mode: self._ui_events.put(PressEvent(mode)))
            threading.Thread(target=self.ui.render_loop, daemon=True).start()

        log.info("[FSM] ══════════════════ FSM starting ══════════════════")
        try:
            while self._state not in (State.EXIT,):
                self._tick()
        except KeyboardInterrupt:
            log.warning("[FSM] KeyboardInterrupt – cleanup")
            self._go(State.CLEANUP)
            self._s_cleanup()
        finally:
            self._heartbeat.stop()
            self._cup_thread.stop()
            if self.ui:
                self.ui.stop()
            log.info("[FSM] FSM exited")

    # ─────────────────────────────────────────────────────────────────────────
    # Queue helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _wait_menu_select(self) -> int:
        """Block until a MenuSelectEvent arrives; return the selected index."""
        while True:
            try:
                event = self._ui_events.get(timeout=0.1)
                if isinstance(event, MenuSelectEvent):
                    log.info("[FSM] MenuSelect idx=%d  state=%s",
                             event.index, self._state.name)
                    return event.index
                if isinstance(event, PressEvent) and event.ui_mode == 'info':
                    return -1   # info dismissed – caller re-enters its own state
            except queue.Empty:
                if self._state in (State.EXIT, State.CLEANUP):
                    return -1

    def _wait_press(self, expected_modes: tuple[str, ...]) -> PressEvent | None:
        """Block until a PressEvent with one of the expected ui_modes arrives."""
        while True:
            try:
                event = self._ui_events.get(timeout=0.1)
                if isinstance(event, PressEvent) and event.ui_mode in expected_modes:
                    return event
            except queue.Empty:
                if self._state in (State.EXIT, State.CLEANUP):
                    return None

    def _drain_queue(self):
        while not self._ui_events.empty():
            try:
                self._ui_events.get_nowait()
            except queue.Empty:
                break

    # ─────────────────────────────────────────────────────────────────────────
    # Tick dispatcher
    # ─────────────────────────────────────────────────────────────────────────

    def _tick(self):
        log.debug("[FSM] tick: %s", self._state.name)
        handlers = {
            State.HOMING:                     self._s_homing,
            State.FETCH_STORE_DATA:           self._s_fetch_store_data,
            State.MAIN_MENU:                  self._s_main_menu,
            State.SETTINGS:                   self._s_settings,
            State.SET_VISCOSITY_LIST:         self._s_set_viscosity_list,
            State.SET_VISCOSITY_EDIT:         self._s_set_viscosity_edit,
            State.SET_DRINK_SLOTS:            self._s_set_drink_slots,
            State.SET_SLOT_INGREDIENT:        self._s_set_slot_ingredient,
            State.MAINTENANCE:                self._s_maintenance,
            State.MOVE_X:                     self._s_move_x,
            State.CALIBRATE_SCALE:            self._s_calibrate_scale,
            State.READ_WEIGHT:                self._s_read_weight,
            State.CLEAN:                      self._s_clean,
            State.CLEAN_SELECT_SPIRIT_SLOT:   self._s_clean_select_spirit_slot,
            State.CLEAN_ENTER_SPIRIT_COUNT:   self._s_clean_enter_spirit_count,
            State.CLEAN_SELECT_MIXER_SLOT:    self._s_clean_select_mixer_slot,
            State.CLEAN_ENTER_MIXER_GRAMS:    self._s_clean_enter_mixer_grams,
            State.VERIFY_CUP_MOUNTED:         self._s_verify_cup_mounted,
            State.START_CUP_THREAD_CLEAN:     self._s_start_cup_thread_clean,
            State.RUN_IDLE:                   self._s_run_idle,
            State.WAIT_FOR_CUP_ADDITION:      self._s_wait_for_cup_addition,
            State.CHECK_ORDER:                self._s_check_order,
            State.MOVE_TO_POS:                self._s_move_to_pos,
            State.POUR_SPIRIT:                self._s_pour_spirit,
            State.POUR_MIXER:                 self._s_pour_mixer,
            State.CHECK_NEXT_POSITION:        self._s_check_next_position,
            State.MOVE_TO_IDLE:               self._s_move_to_idle,
            State.MIXER_ERROR:                self._s_mixer_error,
            State.VERIFY_CUP_BEFORE_RESUME:   self._s_verify_cup_before_resume,
            State.STOP:                       self._s_stop,
            State.WAIT_FOR_CUP_ADDED:         self._s_wait_for_cup_added,
            State.DISARM_EMERGENCY_STOP:      self._s_disarm_emergency_stop,
            State.EMERGENCY_RECOVERY_ROUTING: self._s_emergency_recovery_routing,
            State.REPEAT_CURRENT_DRINK:       self._s_repeat_current_drink,
            State.WAIT_FOR_CUP_REMOVAL:       self._s_wait_for_cup_removal,
            State.AFTER_CUP_REMOVAL_ROUTING:  self._s_after_cup_removal_routing,
            State.WAIT_FOR_NEW_CUP_ADDED:     self._s_wait_for_new_cup_added,
            State.CHECK_FOR_OTHER_DRINKS:     self._s_check_for_other_drinks,
            State.CLEANUP:                    self._s_cleanup,
        }
        handler = handlers.get(self._state)
        if handler is None:
            log.error("[FSM] No handler for %s", self._state.name)
            self._go(State.CLEANUP)
            return
        try:
            handler()
        except Exception as exc:
            log.exception("[FSM] Exception in %s: %s", self._state.name, exc)
            self._go(State.CLEANUP)

    def _go(self, new_state: State, reason: str = ""):
        msg = f"[FSM] ▶ {self._state.name} → {new_state.name}"
        if reason:
            msg += f"  ({reason})"
        log.info(msg)
        self._state = new_state

    # ─────────────────────────────────────────────────────────────────────────
    # ── Startup ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_homing(self):
        log.info("[FSM:HOMING] Starting homing sequence")
        self.led.set("idle")
        if self.ui:
            self.ui.show_working("Homing...")
        self.machine.homing()
        if self.ui:
            self.ui.set_work_done("Homing complete!")
        log.info("[FSM:HOMING] Done")
        time.sleep(2.0)
        self._go(State.FETCH_STORE_DATA)

    def _s_fetch_store_data(self):
        log.info("[FSM:FETCH_STORE_DATA] Fetching store data")
        if self.ui:
            self.ui.show_working("Fetching...")
        try:
            self.repo.fetch_all_store_data()
            if self.ui:
                self.ui.set_work_done("Store loaded!")
        except Exception as exc:
            log.warning("[FSM:FETCH_STORE_DATA] Failed: %s", exc)
            if self.ui:
                self.ui.set_work_done(f"Err: {str(exc)[:14]}")
        time.sleep(2.0)
        self._go(State.MAIN_MENU)

    # ─────────────────────────────────────────────────────────────────────────
    # ── Main Menu ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_main_menu(self):
        log.info("[FSM:MAIN_MENU] Showing main menu")
        self.led.set("idle")
        self._mode = Mode.RUN
        self._drain_queue()
        if self.ui:
            self.ui.show_menu('BARBOT', [
                {'label': 'Setup',       'hint': f'{ARROW} '},
                {'label': 'Run',         'hint': '  '},
                {'label': 'Maintenance', 'hint': f'{ARROW} '},
                {'label': 'Quit',        'hint': '  '},
            ])
        idx = self._wait_menu_select()
        targets = [State.SETTINGS, State.RUN_IDLE, State.MAINTENANCE, State.CLEANUP]
        if 0 <= idx < len(targets):
            self._go(targets[idx])

    # ─────────────────────────────────────────────────────────────────────────
    # ── Settings ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_settings(self):
        log.info("[FSM:SETTINGS] Showing settings menu")
        self._drain_queue()
        if self.ui:
            self.ui.show_menu('Setup', [
                {'label': 'Fetch',     'hint': '  '},
                {'label': 'Status',    'hint': f'{ARROW} '},
                {'label': 'Slots',     'hint': f'{ARROW} '},
                {'label': 'Viscosity', 'hint': f'{ARROW} '},
                {'label': 'Back',      'hint': '  '},
            ])
        idx = self._wait_menu_select()
        if idx == 0:
            self._go(State.FETCH_STORE_DATA, "manual fetch")
        elif idx == 1:
            self._show_status_info()
            self._go(State.SETTINGS)
        elif idx == 2:
            self._go(State.SET_DRINK_SLOTS)
        elif idx == 3:
            self._go(State.SET_VISCOSITY_LIST)
        elif idx == 4:
            self._go(State.MAIN_MENU, "back")

    def _show_status_info(self):
        def disp(s): return self.repo.slug_to_name(s) if s else '---'
        lines = ['-- Spirit Slots ----']
        for slot in SPIRIT_SLOTS:
            ing  = self.repo.slot_ingredient(slot)
            flag = '[E]' if slot in self.repo.empty_slots else ''
            lines.append(f'{slot}: {disp(ing)} {flag}'.rstrip())
        lines.append('-- Mixer Slots -----')
        for slot in MIXER_SLOTS:
            ing  = self.repo.slot_ingredient(slot)
            flag = '[E]' if slot in self.repo.empty_slots else ''
            lines.append(f'{slot}: {disp(ing)} {flag}'.rstrip())
        lines += [
            '-- Store -----------',
            f'Fetched: {self.repo.last_fetched or "never"}',
            f'Products: {len(self.repo.products)}',
            f'Spirits:  {len(self.repo.available_spirits)}',
            f'Mixers:   {len(self.repo.available_mixers)}',
            '', '  [Press to return] ',
        ]
        if self.ui:
            self.ui.show_info('Status', lines)
        self._wait_press(('info',))

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _s_set_drink_slots(self):
        log.info("[FSM:SET_DRINK_SLOTS] Showing slot list")
        self._drain_queue()
        items = [
            {'label': f'{s}: {self.repo.slug_to_name(self.repo.slot_ingredient(s)) if self.repo.slot_ingredient(s) else "---"}',
             'hint': f'{ARROW} '}
            for s in ALL_SLOTS
        ]
        items.append({'label': 'Back', 'hint': '  '})
        if self.ui:
            self.ui.show_menu('Slots', items)
        idx = self._wait_menu_select()
        if idx < len(ALL_SLOTS):
            self._edit_slot = ALL_SLOTS[idx]
            self._go(State.SET_SLOT_INGREDIENT)
        else:
            self._go(State.SETTINGS, "back")

    def _s_set_slot_ingredient(self):
        slot      = self._edit_slot
        is_spirit = self.repo.config.is_spirit_slot(slot)
        slugs     = sorted(
            self.repo.available_spirits if is_spirit else self.repo.available_mixers)
        assigned  = {ing for s, ing in self.repo.slot_mapping.items()
                     if s != slot and ing}
        available = [s for s in slugs if s not in assigned]
        cur_ing   = self.repo.slot_ingredient(slot)
        items     = [{'label': self.repo.slug_to_name(s), 'hint': '  '} for s in available]
        if cur_ing:
            items.append({'label': f'Clear ({self.repo.slug_to_name(cur_ing)})', 'hint': '  '})
        items.append({'label': 'Cancel', 'hint': '  '})
        self._slot_edit_meta = {'slugs': available, 'has_clear': bool(cur_ing)}
        self._drain_queue()
        if self.ui:
            self.ui.show_menu(slot, items)
        idx       = self._wait_menu_select()
        available = self._slot_edit_meta['slugs']
        has_clear = self._slot_edit_meta['has_clear']
        if idx < len(available):
            self.repo.set_slot(slot, available[idx])
            self._do_work(f'Sync {slot}...', self.inventory.push_all)
            self._go(State.SET_DRINK_SLOTS)
        elif has_clear and idx == len(available):
            self.repo.clear_slot(slot)
            self._do_work(f'Sync {slot}...', self.inventory.push_all)
            self._go(State.SET_DRINK_SLOTS)
        else:
            self._go(State.SET_DRINK_SLOTS, "cancel")

    # ── Viscosity ──────────────────────────────────────────────────────────────

    def _s_set_viscosity_list(self):
        self._visc_attr_entries = [
            (slug, label)
            for slug, label in [('pa_spirits', 'Spirits'), ('pa_mixers', 'Mixers')]
            if self._extract_viscosity(slug)
        ]
        items = [{'label': label, 'hint': f'{ARROW} '} for _, label in self._visc_attr_entries]
        if not items:
            items.append({'label': '(Fetch first)', 'hint': '  '})
        items.append({'label': 'Back', 'hint': '  '})
        self._drain_queue()
        if self.ui:
            self.ui.show_menu('Viscosity', items)
        idx = self._wait_menu_select()
        if idx < len(self._visc_attr_entries):
            attr_slug, label = self._visc_attr_entries[idx]
            self._edit_visc_data = self._extract_viscosity(attr_slug)
            visc_items = [
                {'label': f'{name} ({d["viscosity"]:.1f})', 'hint': f'{ARROW} '}
                for name, d in self._edit_visc_data.items()
            ]
            visc_items.append({'label': 'Back', 'hint': '  '})
            if self.ui:
                self.ui.show_menu(label, visc_items)
            self._go(State.SET_VISCOSITY_EDIT)
        else:
            self._go(State.SETTINGS, "back")

    def _s_set_viscosity_edit(self):
        self._drain_queue()
        idx   = self._wait_menu_select()
        names = list(self._edit_visc_data.keys())
        if idx < len(names):
            self._edit_visc_bottle = names[idx]
            data = self._edit_visc_data[names[idx]]
            if self.ui:
                self.ui.show_visc_edit(self._edit_visc_bottle, data['viscosity'])
            event = self._wait_press(('visc_edit',))
            if event and self.ui:
                val     = self.ui.visc_val
                attr_id = self.repo.attribute_ids.get(data['attr_slug'])
                term_id = data.get('term_id')
                self.repo.save_viscosity_local(data['attr_slug'], data['term_slug'], val)
                if attr_id and term_id:
                    self._do_work(
                        f'Saving {self._edit_visc_bottle[:12]}...',
                        lambda: self.repo.update_term_viscosity(attr_id, term_id, val),
                    )
            self._go(State.SET_VISCOSITY_LIST)
        else:
            self._go(State.SET_VISCOSITY_LIST, "back")

    # ─────────────────────────────────────────────────────────────────────────
    # ── Maintenance ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_maintenance(self):
        log.info("[FSM:MAINTENANCE] Showing maintenance menu")
        self.led.set("idle")
        self._drain_queue()
        if self.ui:
            self.ui.show_menu('Maintenance', [
                {'label': 'Move X',         'hint': '  '},
                {'label': 'Read Weight',     'hint': '  '},
                {'label': 'Calibrate Scale', 'hint': '  '},
                {'label': 'Clean',           'hint': f'{ARROW} '},
                {'label': 'Back',            'hint': '  '},
            ])
        idx = self._wait_menu_select()
        targets = [
            State.MOVE_X, State.READ_WEIGHT, State.CALIBRATE_SCALE,
            State.CLEAN,  State.MAIN_MENU,
        ]
        if 0 <= idx < len(targets):
            self._go(targets[idx])

    def _s_move_x(self):
        log.info("[FSM:MOVE_X] X-move mode")
        self._drain_queue()
        if self.ui:
            self.ui.show_x_move(self.machine.x_position, self.machine.x_max)
        event = self._wait_press(('x_move',))
        if event and self.ui:
            target = self.ui.xmove_target
            log.info("[FSM:MOVE_X] Moving to %d", target)
            self._do_work(f'Moving to {target}...', lambda: self.machine.move_to(target))
        self._go(State.MAINTENANCE)

    def _s_calibrate_scale(self):
        log.info("[FSM:CALIBRATE_SCALE] Taring scale")
        self._do_work("Taring scale...", self.machine.tare_scale)
        grams = self.machine.read_weight()
        log.info("[FSM:CALIBRATE_SCALE] Reading after tare: %.2fg", grams)
        self._go(State.MAINTENANCE)

    def _s_read_weight(self):
        grams = self.machine.read_weight()
        log.info("[FSM:READ_WEIGHT] %.2fg", grams)
        if self.ui:
            self.ui.show_info('Weight', [f'{grams:.2f} g', '', '[Press to return]'])
        self._wait_press(('info',))
        self._go(State.MAINTENANCE)

    # ─────────────────────────────────────────────────────────────────────────
    # ── Clean ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_clean(self):
        self._drain_queue()
        if self.ui:
            self.ui.show_menu('Clean', [
                {'label': 'Spirit', 'hint': f'{ARROW} '},
                {'label': 'Mixer',  'hint': f'{ARROW} '},
                {'label': 'Back',   'hint': '  '},
            ])
        idx = self._wait_menu_select()
        if idx == 0:
            self._go(State.CLEAN_SELECT_SPIRIT_SLOT)
        elif idx == 1:
            self._go(State.CLEAN_SELECT_MIXER_SLOT)
        else:
            self._go(State.MAINTENANCE, "back")

    def _s_clean_select_spirit_slot(self):
        self._drain_queue()
        items = [{'label': s.replace('_', ' '), 'hint': f'{ARROW} '} for s in SPIRIT_SLOTS]
        items.append({'label': 'Back', 'hint': '  '})
        if self.ui:
            self.ui.show_menu('Spirit Slot', items)
        idx = self._wait_menu_select()
        if idx < len(SPIRIT_SLOTS):
            self._clean_slot = SPIRIT_SLOTS[idx]
            self._go(State.CLEAN_ENTER_SPIRIT_COUNT)
        else:
            self._go(State.CLEAN, "back")

    def _s_clean_enter_spirit_count(self):
        self._drain_queue()
        if self.ui:
            self.ui.show_num_entry(
                'Clean Spirit', f'Slot: {self._clean_slot[-1]}',
                'Count:', 1, 99, 1, None,
            )
        event = self._wait_press(('num_entry',))
        if event and self.ui:
            val = self.ui.nentry_val
            log.info("[FSM:CLEAN] Spirit count=%d  slot=%s", val, self._clean_slot)
            self._clean_steps = [DispenseStep(
                kind="spirit", slot=self._clean_slot,
                ingredient="clean-fluid", pours=val, viscosity=1.0,
            )]
            self._mode = Mode.CLEAN
            self._go(State.VERIFY_CUP_MOUNTED)
        else:
            self._go(State.CLEAN, "cancel")

    def _s_clean_select_mixer_slot(self):
        self._drain_queue()
        items = [{'label': s.replace('_', ' '), 'hint': f'{ARROW} '} for s in MIXER_SLOTS]
        items.append({'label': 'Back', 'hint': '  '})
        if self.ui:
            self.ui.show_menu('Mixer Slot', items)
        idx = self._wait_menu_select()
        if idx < len(MIXER_SLOTS):
            self._clean_slot = MIXER_SLOTS[idx]
            self._go(State.CLEAN_ENTER_MIXER_GRAMS)
        else:
            self._go(State.CLEAN, "back")

    def _s_clean_enter_mixer_grams(self):
        self._drain_queue()
        if self.ui:
            self.ui.show_num_entry(
                'Clean Mixer', f'Slot: {self._clean_slot[-1]}',
                'Weight (g):', 1, 999, 1, 10,
            )
        event = self._wait_press(('num_entry',))
        if event and self.ui:
            val = self.ui.nentry_val
            log.info("[FSM:CLEAN] Mixer grams=%d  slot=%s", val, self._clean_slot)
            self._clean_steps = [DispenseStep(
                kind="mixer", slot=self._clean_slot,
                ingredient="clean-fluid", grams=float(val),
            )]
            self._mode = Mode.CLEAN
            self._go(State.VERIFY_CUP_MOUNTED)
        else:
            self._go(State.CLEAN, "cancel")

    # ─────────────────────────────────────────────────────────────────────────
    # ── Clean cycle: cup check + arm ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_verify_cup_mounted(self):
        log.info("[FSM:VERIFY_CUP_MOUNTED] Checking cup before clean")
        self.led.set("cup_missing")
        if self.ui:
            self.ui.show_working("Place cup...")
        if not self.machine.cup_present():
            log.warning("[FSM:VERIFY_CUP_MOUNTED] Cup missing – sim: auto-place")
            self.machine.simulate_cup_placed()
            time.sleep(0.3)
        if self.machine.cup_present():
            self._go(State.START_CUP_THREAD_CLEAN, "cup mounted")

    def _s_start_cup_thread_clean(self):
        log.info("[FSM:START_CUP_THREAD_CLEAN] Arming emergency-stop")
        self._emergency_stop.clear()
        self._cup_thread.activate()
        self._steps      = list(self._clean_steps)
        self._step_index = 0
        self._go(State.MOVE_TO_POS, "start cleaning")

    # ─────────────────────────────────────────────────────────────────────────
    # ── Run idle / order polling ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_run_idle(self):
        log.info("[FSM:RUN_IDLE] Idle")
        self.led.set("idle")
        self._mode = Mode.RUN
        if self.ui:
            self.ui.show_run(self._run_count, None)

        if not self.machine.cup_present():
            self._go(State.WAIT_FOR_CUP_ADDITION, "cup absent")
            return
        self._go(State.CHECK_ORDER)

    def _s_wait_for_cup_addition(self):
        log.info("[FSM:WAIT_FOR_CUP_ADDITION] Waiting for cup")
        self.led.set("cup_missing")
        self.machine.simulate_cup_placed()
        time.sleep(0.3)
        if self.machine.cup_present():
            self._go(State.RUN_IDLE, "cup placed")

    def _s_check_order(self):
        log.info("[FSM:CHECK_ORDER] Polling orders")
        try:
            orders = self.repo.fetch_orders()
        except Exception as exc:
            log.warning("[FSM:CHECK_ORDER] API error: %s", exc)
            self._go(State.RUN_IDLE, "API error")
            time.sleep(2)
            return

        if not orders:
            log.info("[FSM:CHECK_ORDER] No pending orders")
            self._go(State.RUN_IDLE, "no order")
            time.sleep(self.repo.poll_interval)
            return

        order = orders[0]
        self._order_id = order["id"]
        log.info("[FSM:CHECK_ORDER] Order #%d", self._order_id)

        specs: list[DrinkSpec] = []
        for item in order.get("line_items", []):
            specs.extend(self.resolver.resolve(item))

        if not specs:
            log.warning("[FSM:CHECK_ORDER] No resolvable drinks")
            self._go(State.RUN_IDLE, "no drinks")
            return

        self._order_specs = specs
        self._drink_index = 0
        log.info("[FSM:CHECK_ORDER] %d drink(s)", len(specs))
        self._load_drink(0)
        self._go(State.MOVE_TO_POS, "order found")

    # ─────────────────────────────────────────────────────────────────────────
    # ── Emergency Zone (pouring) ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_move_to_pos(self):
        if not self._steps:
            self._go(State.MOVE_TO_IDLE)
            return
        self._current_step = self._steps[self._step_index]
        step = self._current_step
        log.info("[FSM:MOVE_TO_POS] Step %d/%d – %s",
                 self._step_index + 1, len(self._steps), step.label())
        self.led.set("pouring")
        if self._emergency_stop.is_set():
            self._go(State.STOP, "emergency stop")
            return
        try:
            self.machine.move_to_slot(step.slot)
        except ValueError as exc:
            log.error("[FSM:MOVE_TO_POS] %s – skipping", exc)
            self._step_index += 1
            if self._step_index >= len(self._steps):
                self._go(State.MOVE_TO_IDLE, "slot error")
            return
        self._go(State.POUR_SPIRIT if step.kind == "spirit" else State.POUR_MIXER)

    def _s_pour_spirit(self):
        step = self._current_step
        log.info("[FSM:POUR_SPIRIT] %s", step.label())
        if self._emergency_stop.is_set():
            self._go(State.STOP, "cup removed")
            return
        self.machine.pour_spirit(step.slot, step.pours, step.viscosity)
        if self._emergency_stop.is_set():
            self._go(State.STOP, "cup removed after pour")
        else:
            self._go(State.CHECK_NEXT_POSITION, "done")

    def _s_pour_mixer(self):
        step = self._current_step
        log.info("[FSM:POUR_MIXER] %s", step.label())
        if self._emergency_stop.is_set():
            self._go(State.STOP, "cup removed")
            return
        try:
            self.machine.pour_mixer(step.slot, step.grams)
        except MixerStall as exc:
            log.warning("[FSM:POUR_MIXER] Stall: %s", exc)
            self.machine.stop_pump()
            self._go(State.MIXER_ERROR, "bottle empty or pipe blocked")
            return
        if self._emergency_stop.is_set():
            self._go(State.STOP, "cup removed after pour")
        else:
            self._go(State.CHECK_NEXT_POSITION, "done")

    def _s_check_next_position(self):
        next_i = self._step_index + 1
        if next_i < len(self._steps):
            self._step_index = next_i
            self._go(State.MOVE_TO_POS, "next position")
        else:
            self._go(State.MOVE_TO_IDLE, "finished")

    def _s_move_to_idle(self):
        log.info("[FSM:MOVE_TO_IDLE] Returning to idle")
        self.led.set("finished")
        self.machine.move_to_idle()
        self._cup_thread.deactivate()
        self._go(State.WAIT_FOR_CUP_REMOVAL, "idle reached")

    # ─────────────────────────────────────────────────────────────────────────
    # ── Mixer warning flow ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_mixer_error(self):
        log.warning("[FSM:MIXER_ERROR] Bottle empty or pipe stuck")
        self.led.set("warning")
        if self.ui:
            self.ui.show_info('Mixer Error',
                              ['Bottle empty or', 'pipe stuck!', 'Fix & press OK', ''])
        self._wait_press(('info',))
        self._go(State.VERIFY_CUP_BEFORE_RESUME)

    def _s_verify_cup_before_resume(self):
        log.info("[FSM:VERIFY_CUP_BEFORE_RESUME] Checking cup")
        self.led.set("cup_missing")
        if not self.machine.cup_present():
            self.machine.simulate_cup_placed()
            time.sleep(0.3)
        if self.machine.cup_present():
            current_g = self.machine.read_weight()
            remaining = max(0.0, self._current_step.grams - current_g)
            log.info("[FSM:VERIFY_CUP_BEFORE_RESUME] Resuming – %.1fg remaining", remaining)
            self._current_step = DispenseStep(
                kind="mixer", slot=self._current_step.slot,
                ingredient=self._current_step.ingredient, grams=remaining,
            )
            self.led.set("pouring")
            self._go(State.POUR_MIXER, "resume")

    # ─────────────────────────────────────────────────────────────────────────
    # ── Emergency stop flow ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_stop(self):
        log.error("[FSM:STOP] !! EMERGENCY STOP !!")
        self.led.set("emergency")
        self.machine.stop_pump()
        self.machine.move_to_idle()
        if self.ui:
            self.ui.show_info('EMERGENCY STOP',
                              ['Cup removed!', 'Replace cup', 'to continue', ''])
        self._go(State.WAIT_FOR_CUP_ADDED, "halted")

    def _s_wait_for_cup_added(self):
        log.info("[FSM:WAIT_FOR_CUP_ADDED] Waiting for cup")
        self.led.set("cup_missing")
        self.machine.simulate_cup_placed()
        time.sleep(0.5)
        if self.machine.cup_present():
            self._go(State.DISARM_EMERGENCY_STOP, "cup added")

    def _s_disarm_emergency_stop(self):
        log.info("[FSM:DISARM_EMERGENCY_STOP] Clearing emergency stop")
        self._emergency_stop.clear()
        self._go(State.EMERGENCY_RECOVERY_ROUTING)

    def _s_emergency_recovery_routing(self):
        if self._mode == Mode.RUN:
            self._go(State.REPEAT_CURRENT_DRINK, "run mode")
        else:
            self._go(State.MAINTENANCE, "clean mode – abort")

    def _s_repeat_current_drink(self):
        log.info("[FSM:REPEAT_CURRENT_DRINK] Reloading drink #%d", self._drink_index + 1)
        self._load_drink(self._drink_index)
        self._cup_thread.activate()
        self._go(State.MOVE_TO_POS, "resume")

    # ─────────────────────────────────────────────────────────────────────────
    # ── Normal completion flow ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_wait_for_cup_removal(self):
        log.info("[FSM:WAIT_FOR_CUP_REMOVAL] Waiting for cup removal")
        self.led.set("finished")
        self.machine.simulate_cup_removed()
        time.sleep(0.5)
        if not self.machine.cup_present():
            self._go(State.AFTER_CUP_REMOVAL_ROUTING, "removed")

    def _s_after_cup_removal_routing(self):
        if self._mode == Mode.RUN:
            self._go(State.WAIT_FOR_NEW_CUP_ADDED)
        else:
            self._go(State.MAINTENANCE, "clean done")

    def _s_wait_for_new_cup_added(self):
        log.info("[FSM:WAIT_FOR_NEW_CUP_ADDED] Waiting for fresh cup")
        self.led.set("cup_missing")
        self.machine.simulate_cup_placed()
        time.sleep(0.4)
        if self.machine.cup_present():
            self._go(State.CHECK_FOR_OTHER_DRINKS)

    def _s_check_for_other_drinks(self):
        next_i = self._drink_index + 1
        log.info("[FSM:CHECK_FOR_OTHER_DRINKS] Drink %d/%d done",
                 self._drink_index + 1, len(self._order_specs))
        if next_i < len(self._order_specs):
            self._drink_index = next_i
            self._load_drink(next_i)
            self._cup_thread.activate()
            self._go(State.MOVE_TO_POS, "next drink")
        else:
            self._complete_order()
            self._go(State.RUN_IDLE, "order finished")

    # ─────────────────────────────────────────────────────────────────────────
    # ── Cleanup ──
    # ─────────────────────────────────────────────────────────────────────────

    def _s_cleanup(self):
        log.info("[FSM:CLEANUP] Global cleanup")
        self.led.set("emergency")
        try:
            self.machine.stop_pump()
        except Exception:
            pass
        try:
            self.machine.move_to_idle()
        except Exception:
            pass
        self._cup_thread.deactivate()
        self.led.set("idle")
        if self.ui:
            self.ui.shutdown()
        self._go(State.EXIT, "clean exit")

    # ─────────────────────────────────────────────────────────────────────────
    # ── Internal helpers ──
    # ─────────────────────────────────────────────────────────────────────────

    def _load_drink(self, index: int):
        drink = self._order_specs[index]
        log.info("[FSM] Loading drink %d/%d '%s'  (order #%s)",
                 index + 1, len(self._order_specs), drink.name, self._order_id or '?')
        drink.log()
        self._steps      = steps_from_spec(drink)
        self._step_index = 0
        self._emergency_stop.clear()
        self._cup_thread.activate()
        if self.ui:
            self.ui.show_mixing(index + 1, len(self._order_specs), drink.name)

    def _complete_order(self):
        if self._order_id is None:
            return
        self._run_count += 1
        if self.ui:
            self.ui.update_run(self._run_count, self._order_id)
        try:
            self.repo.complete_order(self._order_id)
        except Exception as exc:
            log.warning("[FSM] Failed to complete order #%d: %s", self._order_id, exc)
        finally:
            if self.ui:
                self.ui.clear_mixing()
            self._order_id    = None
            self._order_specs = []

    def _do_work(self, title: str, fn, *args):
        """Show working screen, run fn synchronously, show result."""
        if self.ui:
            self.ui.show_working(title)
        try:
            result = fn(*args)
            msg = result if isinstance(result, str) else 'Done!'
        except Exception as exc:
            msg = f'Err: {str(exc)[:14]}'
        log.info("[FSM:WORK] '%s' → %s", title, msg)
        if self.ui:
            self.ui.set_work_done(msg)
        time.sleep(2.2)

    def _extract_viscosity(self, attr_slug: str) -> dict:
        result = {}
        for tslg, data in self.repo.bottle_properties.get(attr_slug, {}).items():
            result[data.get('name', tslg)] = {
                'viscosity': data.get('viscosity', 1),
                'term_id':   data.get('id'),
                'term_slug': tslg,
                'attr_slug': attr_slug,
            }
        return result
