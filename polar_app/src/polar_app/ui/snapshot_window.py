"""Окреме вікно «Знімок»: збережений знімок камери для її поточного положення.

Коли камера переходить в інше положення (кнопки кроку, Навчання, будь-яке керування), у вікні
показується знімок відповідного вузла сітки; якщо для положення знімка ще немає — про це сказано.
"""
from __future__ import annotations

import datetime as dt
import os

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget


class SnapshotWindow(QWidget):
    def __init__(self, ctx, name: str, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.ctx = ctx
        self.name = name
        self.path = None             # показаний файл
        self._mtime = None
        self._pixmap = None
        self.setWindowTitle(f"Знімок · {name}")
        self.resize(800, 520)
        v = QVBoxLayout(self)
        self.image = QLabel("")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image.setMinimumSize(320, 180)
        self.image.setStyleSheet("background: #111; color: #bbb")
        v.addWidget(self.image, 1)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        v.addWidget(self.info)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.refresh()

    def refresh(self) -> None:
        found = self.ctx.learning.snapshot_for(self.name)
        if found is None:
            self.info.setText("Немає кутів камери (немає звʼязку з сервоприводами) — показано останній знімок."
                              if self.path else "Немає кутів камери (немає звʼязку з сервоприводами).")
            return
        path, entry, az, el = found
        pose = f"Камера: азимут {az:.1f}°, кут місця {el:.1f}°"
        if path is None:
            self._show(None)
            self.info.setText(f"{pose} · для цього положення знімка ще немає (Навчання або «Зберегти»).")
            return
        self._show(path)
        when = entry.get("time", "")
        try:
            when = dt.datetime.fromisoformat(when).astimezone().strftime("%d.%m.%Y %H:%M:%S")
        except ValueError:
            pass
        kind = " · збережено вручну" if entry.get("manual") else ""
        self.info.setText(f"{pose}\nЗнімок: вузол азимут {entry['azimuth_node']:g}°, кут місця "
                          f"{entry['elevation_node']:g}° (фактично {entry['azimuth_deg']:.1f}° / "
                          f"{entry['elevation_deg']:.1f}°) · {when}{kind}\n{path}")

    def _show(self, path) -> None:
        mtime = os.path.getmtime(path) if path else None
        if path == self.path and mtime == self._mtime:
            return
        self.path, self._mtime = path, mtime
        self._pixmap = QPixmap(path) if path else None
        if self._pixmap is None or self._pixmap.isNull():
            self._pixmap = None
            self.image.setPixmap(QPixmap())
            self.image.setText("Знімка для цього положення немає")
        self._fit()

    def _fit(self) -> None:
        if self._pixmap is not None:
            self.image.setPixmap(self._pixmap.scaled(self.image.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._fit()

    def closeEvent(self, e) -> None:  # noqa: N802
        self.timer.stop()
        super().closeEvent(e)
