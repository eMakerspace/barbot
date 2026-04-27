"""Rotary encoder with push button.

Uses a quadrature state machine so that one mechanical detent always
produces exactly one rotation event, regardless of contact bounce or
partial knob movement.

State encoding:  state = (CLK << 1) | DT   →   0b00 .. 0b11

Transition table (row = previous state, col = current state):
         curr: 00   01   10   11
  prev 00:      0   -1   +1    0
  prev 01:     +1    0    0   -1
  prev 10:     -1    0    0   +1
  prev 11:      0   +1   -1    0

A typical mechanical encoder produces 4 half-step transitions per
detent, so the accumulator fires at ±STEPS_PER_DETENT (default 4).
"""

import threading
import time
from typing import Callable

import RPi.GPIO as GPIO


class RotaryEncoder:
    """Quadrature rotary encoder + push-button HAL.

    Parameters
    ----------
    pin_clk, pin_dt, pin_sw:
        BCM GPIO numbers for CLK, DT, and the push-button switch.
    on_rotate:
        Callable(direction: int) – called with +1 (CW) or -1 (CCW)
        for each full detent.
    on_press:
        Callable() – called on a debounced button press.
    steps_per_detent:
        Number of quadrature half-steps that make one detent.
        Most mechanical encoders use 4; set to 2 or 1 for encoders
        with fewer pulses per click.
    debounce_s:
        Minimum seconds between successive button-press events.
    poll_hz:
        Polling frequency in Hz.  2000 (2 kHz) is a good default.
    """

    # Transition table: _TABLE[prev][curr] → delta
    _TABLE: tuple[tuple[int, ...], ...] = (
        ( 0, -1, +1,  0),   # prev = 0b00
        (+1,  0,  0, -1),   # prev = 0b01
        (-1,  0,  0, +1),   # prev = 0b10
        ( 0, +1, -1,  0),   # prev = 0b11
    )

    def __init__(
        self,
        pin_clk: int,
        pin_dt: int,
        pin_sw: int,
        on_rotate: Callable[[int], None],
        on_press: Callable[[], None],
        *,
        steps_per_detent: int = 4,
        debounce_s: float = 0.3,
        poll_hz: int = 2000,
    ) -> None:
        self._pin_clk = pin_clk
        self._pin_dt  = pin_dt
        self._pin_sw  = pin_sw
        self._on_rotate = on_rotate
        self._on_press  = on_press
        self._steps_per_detent = steps_per_detent
        self._debounce_s = debounce_s
        self._poll_interval = 1.0 / poll_hz

        self._alive   = False
        self._thread: threading.Thread | None = None

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        """Configure GPIO pins and launch the polling thread."""
        try:
            GPIO.setmode(GPIO.BCM)
        except RuntimeError:
            pass
        GPIO.setup(self._pin_clk, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self._pin_dt,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self._pin_sw,  GPIO.IN, pull_up_down=GPIO.PUD_UP)

        self._alive  = True
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the polling thread (non-blocking; does not clean up GPIO)."""
        self._alive = False

    # ── Internal polling loop ────────────────────────────────────

    def _poll(self) -> None:
        last_state  = (GPIO.input(self._pin_clk) << 1) | GPIO.input(self._pin_dt)
        last_sw     = GPIO.input(self._pin_sw)
        accumulator = 0
        btn_time    = 0.0

        while self._alive:
            clk = GPIO.input(self._pin_clk)
            dt  = GPIO.input(self._pin_dt)
            sw  = GPIO.input(self._pin_sw)

            # --- quadrature state machine ---
            curr_state = (clk << 1) | dt
            if curr_state != last_state:
                accumulator += self._TABLE[last_state][curr_state]
                last_state   = curr_state
                if accumulator >= self._steps_per_detent:
                    self._on_rotate(+1)
                    accumulator = 0
                elif accumulator <= -self._steps_per_detent:
                    self._on_rotate(-1)
                    accumulator = 0

            # --- push button: falling edge + debounce ---
            if last_sw == 1 and sw == 0:
                now = time.monotonic()
                if now - btn_time >= self._debounce_s:
                    btn_time = now
                    self._on_press()
            last_sw = sw

            time.sleep(self._poll_interval)
