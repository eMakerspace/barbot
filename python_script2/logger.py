"""Unified logging system with detailed serial output tracking."""

import sys
import time
from datetime import datetime


class SerialLogger:
    """Central logging facility that writes to stderr and ensures serial visibility."""

    def __init__(self):
        self.start_time = time.time()

    def _format_timestamp(self) -> str:
        """Return elapsed seconds since logger creation."""
        elapsed = time.time() - self.start_time
        return f"[{elapsed:8.2f}s]"

    def debug(self, module: str, msg: str):
        """Debug-level message."""
        ts = self._format_timestamp()
        line = f"{ts} [DBG] [{module:12}] {msg}"
        print(line, file=sys.stderr)

    def info(self, module: str, msg: str):
        """Info-level message."""
        ts = self._format_timestamp()
        line = f"{ts} [INF] [{module:12}] {msg}"
        print(line, file=sys.stderr)

    def warn(self, module: str, msg: str):
        """Warning-level message."""
        ts = self._format_timestamp()
        line = f"{ts} [WRN] [{module:12}] {msg}"
        print(line, file=sys.stderr)

    def error(self, module: str, msg: str):
        """Error-level message."""
        ts = self._format_timestamp()
        line = f"{ts} [ERR] [{module:12}] {msg}"
        print(line, file=sys.stderr)

    def critical(self, module: str, msg: str):
        """Critical-level message."""
        ts = self._format_timestamp()
        line = f"{ts} [CRT] [{module:12}] {msg}"
        print(line, file=sys.stderr)


# Global logger instance
_logger = SerialLogger()


def log_debug(module: str, msg: str):
    """Module: debug message."""
    _logger.debug(module, msg)


def log_info(module: str, msg: str):
    """Module: info message."""
    _logger.info(module, msg)


def log_warn(module: str, msg: str):
    """Module: warning message."""
    _logger.warn(module, msg)


def log_error(module: str, msg: str):
    """Module: error message."""
    _logger.error(module, msg)


def log_critical(module: str, msg: str):
    """Module: critical message."""
    _logger.critical(module, msg)
