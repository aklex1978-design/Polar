"""Повідомлення про помилки: необроблені винятки (і в потоках) не губляться мовчки.

Кожна помилка: запис у файл polar.log поруч із базою (з повним traceback), подія «error» у журналі
бази і — якщо є вікно — повідомлення оператору (через обробник on_error, що викликається в потоці
вікна). Повторювані однакові помилки не засипають оператора вікнами (не частіше ніж раз на 30 с).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from typing import Callable, Optional

log = logging.getLogger("polar")


def setup_log_file(db_path: str) -> str:
    """polar.log поруч із базою (до 2 МБ × 3 файли)."""
    directory = os.path.dirname(os.path.abspath(db_path)) if db_path and db_path != ":memory:" else os.getcwd()
    path = os.path.join(directory, "polar.log")
    for h in list(log.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            log.removeHandler(h)
            h.close()
    try:
        h = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return ""
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    return path


class ErrorReporter:
    QUIET_S = 30.0

    def __init__(self, ctx=None) -> None:
        self.ctx = ctx
        self.on_error: Optional[Callable[[str, str], None]] = None     # (коротко, подробиці)
        self._last: dict = {}
        self._prev_hook = None
        self._prev_thread_hook = None

    def install(self) -> None:
        self._prev_hook = sys.excepthook
        self._prev_thread_hook = threading.excepthook
        sys.excepthook = lambda t, v, tb: self.report(t, v, tb)
        threading.excepthook = lambda a: self.report(a.exc_type, a.exc_value, a.exc_traceback,
                                                     getattr(a.thread, "name", ""))

    def uninstall(self) -> None:
        if self._prev_hook is not None:
            sys.excepthook = self._prev_hook
        if self._prev_thread_hook is not None:
            threading.excepthook = self._prev_thread_hook

    def report(self, exc_type, exc, tb, where: str = "") -> str:
        if exc_type is not None and issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            return ""
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        short = f"{exc_type.__name__}: {exc}" if exc_type is not None else str(exc)
        if where:
            short = f"[{where}] {short}"
        log.error("%s\n%s", short, details)
        if self.ctx is not None:
            try:
                with self.ctx.control._db_lock:
                    with self.ctx.conn:
                        self.ctx.conn.execute("INSERT INTO events (camera_id, kind, message) VALUES (NULL, 'error', ?)",
                                              (short[:500],))
            except Exception:  # noqa: BLE001 — журнал недоступний, лишається файл
                pass
        now = time.monotonic()
        if self.on_error is not None and now - self._last.get(short, -1e9) >= self.QUIET_S:
            self._last[short] = now
            try:
                self.on_error(short, details)
            except Exception:  # noqa: BLE001
                pass
        return short
