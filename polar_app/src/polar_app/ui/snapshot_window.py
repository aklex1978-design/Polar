"""Збережений знімок камери для її поточного положення.

SnapshotPanel — віджет (поруч із відео на формі «Обʼєкти управління / Камера»), SnapshotWindow — окреме
вікно «Знімок» (сітка камер). Коли камера переходить в інше положення (кнопки кроку, «Перейти», Навчання),
показується знімок відповідного вузла сітки; якщо для положення знімка ще немає — про це сказано.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget


class SnapshotPanel(QWidget):
    def __init__(self, ctx, name: Optional[str] = None, compact: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.name = name
        self.compact = compact
        self.path = None             # показаний файл
        self._mtime = None
        self._pixmap = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.image = QLabel("")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image.setMinimumSize(200, 120)
        self.image.setStyleSheet("background: #111; color: #bbb")
        self.image.setWordWrap(True)
        v.addWidget(self.image, 1)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        v.addWidget(self.info)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.refresh()

    def set_camera(self, name: Optional[str]) -> None:
        if name != self.name:
            self.name = name
            self._show(None)
            self.refresh()

    def refresh(self) -> None:
        learning = getattr(self.ctx, "learning", None)
        if not self.name or learning is None:
            self._show(None)
            self.info.setText("")
            return
        found = learning.snapshot_for(self.name)
        if found is None:
            self.info.setText("Немає кутів камери (немає звʼязку з сервоприводами) — показано останній знімок."
                              if self.path else "Немає кутів камери (немає звʼязку з сервоприводами).")
            return
        path, entry, az, el = found
        pose = f"Камера: азимут {round(az % 360.0, 1) % 360.0:.1f}°, кут місця {el:.1f}°"
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
        node = (f"вузол азимут {entry['azimuth_node']:.1f}°, кут місця {entry['elevation_node']:.1f}° "
                f"(фактично {entry['azimuth_deg']:.1f}° / {entry['elevation_deg']:.1f}°)")
        if self.compact:
            self.info.setText(f"Знімок: {node} · {when}{kind}")
            self.info.setToolTip(path)
        else:
            self.info.setText(f"{pose}\nЗнімок: {node} · {when}{kind}\n{path}")

    def _show(self, path) -> None:
        mtime = os.path.getmtime(path) if path and os.path.exists(path) else None
        if path == self.path and mtime == self._mtime:
            return
        self.path, self._mtime = path, mtime
        self._pixmap = QPixmap(path) if path else None
        if self._pixmap is None or self._pixmap.isNull():
            self._pixmap = None
            self.image.setPixmap(QPixmap())
            self.image.setText("Знімка для цього положення немає" if self.name else "")
        self._fit()

    def _fit(self) -> None:
        if self._pixmap is not None:
            self.image.setPixmap(self._pixmap.scaled(self.image.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._fit()


class SnapshotWindow(QWidget):
    """Окреме вікно «Знімок» для однієї камери."""

    def __init__(self, ctx, name: str, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.name = name
        self.setWindowTitle(f"Знімок · {name}")
        self.resize(800, 520)
        v = QVBoxLayout(self)
        self.panel = SnapshotPanel(ctx, name)
        self.panel.image.setMinimumSize(320, 180)
        v.addWidget(self.panel)
        self.timer, self.info, self.image = self.panel.timer, self.panel.info, self.panel.image

    @property
    def path(self):
        return self.panel.path

    def refresh(self) -> None:
        self.panel.refresh()

    def closeEvent(self, e) -> None:  # noqa: N802
        self.timer.stop()
        super().closeEvent(e)
