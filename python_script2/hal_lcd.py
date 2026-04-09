"""HAL – 20×4 I²C LCD (HD44780 via PCF8574 backpack).

Wraps RPLCD.i2c.CharLCD with:
  - a row-level write cache that suppresses redundant I²C writes
    (eliminates visible flicker on unchanged rows)
  - a simple backlight-timeout helper
  - all magic constants (address, bus, geometry) in one place
"""

import time

from RPLCD.i2c import CharLCD


LCD_ADDR = 0x3F
I2C_BUS  = 1
COLS     = 20
ROWS     = 4


class LcdDisplay:
    """Thin HAL for the 20×4 I²C character LCD.

    Usage
    -----
        lcd = LcdDisplay()
        lcd.clear()
        lcd.write_row(0, 'Hello, world!')
        lcd.backlight = True
    """

    def __init__(self) -> None:
        self._lcd = CharLCD(
            i2c_expander='PCF8574',
            address=LCD_ADDR,
            port=I2C_BUS,
            cols=COLS,
            rows=ROWS,
            dotsize=8,
            charmap='A02',
            auto_linebreaks=False,
            backlight_enabled=True,
        )
        # Cache of the last string written to each row; avoids redundant I²C
        # transactions which cause visible flicker.
        self._cache: list[str] = [''] * ROWS

    # ── Basic operations ─────────────────────────────────────────

    def clear(self) -> None:
        """Clear the display and invalidate the row cache."""
        self._lcd.clear()
        self._cache = [''] * ROWS

    def write_row(self, row: int, text: str) -> None:
        """Write *text* to *row*, padding / truncating to exactly COLS chars.

        The write is skipped if the formatted string matches the cached value,
        preventing unnecessary I²C traffic and display flicker.
        """
        formatted = f'{text:<{COLS}}'[:COLS]
        if formatted != self._cache[row]:
            self._cache[row] = formatted
            self._lcd.cursor_pos = (row, 0)
            self._lcd.write_string(formatted)

    def invalidate(self) -> None:
        """Force all rows to be rewritten on the next write_row() call."""
        self._cache = [''] * ROWS

    # ── Backlight ────────────────────────────────────────────────

    @property
    def backlight(self) -> bool:
        return self._lcd.backlight_enabled

    @backlight.setter
    def backlight(self, on: bool) -> None:
        self._lcd.backlight_enabled = on
