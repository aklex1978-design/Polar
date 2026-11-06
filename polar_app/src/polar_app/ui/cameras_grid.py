"""«Робота / Камери»: зображення з усіх камер одночасно з позначками калібрування і знайденими обʼєктами."""
from __future__ import annotations

import math
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from polar_core import calibration as cal
from polar_core.angles import wrap360

from .calibration_screen import CameraView
from .common import frame_pixmap


class CameraTile(QWidget):
    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self.name = name
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)
        self.title = QLabel(name)
        self.title.setStyleSheet("font-weight: 600")
        v.addWidget(self.title)
        self.view = CameraView()
        self.view.interactive = False           # лише відображення: без перетягування центру
        self.view.setMinimumSize(320, 180)
        v.addWidget(self.view, 1)


class CamerasGridScreen(QWidget):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.tiles = {}
        self._held = []
        self._last_frame = {}
        v = QVBoxLayout(self)
        title = QLabel("Робота / Камери")
        title.setStyleSheet("font-size: 15px; font-weight: 600")
        v.addWidget(title)
        self.grid = QGridLayout()
        v.addLayout(self.grid, 1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(150)

    def enter(self) -> None:
        recs = self.ctx.records()
        for t in self.tiles.values():
            t.setParent(None)
        self.tiles = {}
        n = max(1, len(recs))
        cols = math.ceil(math.sqrt(n)) if n > 2 else n      # 1, 2×1, 2×2, 3×2 …
        for i, rec in enumerate(recs):
            tile = CameraTile(rec.name)
            self.tiles[rec.name] = tile
            self.grid.addWidget(tile, i // cols, i % cols)
            if self.ctx.video.acquire(rec.name):
                self._held.append(rec.name)
        self.refresh()

    def leave(self) -> None:
        for name in self._held:
            self.ctx.video.release(name)
        self._held = []

    def refresh(self) -> None:
        if not self.isVisible():
            return
        for rec in self.ctx.records():
            tile = self.tiles.get(rec.name)
            if tile is None:
                continue
            view = tile.view
            sf = self.ctx.video.latest(rec.name)
            st = self.ctx.video.stats(rec.name)
            if sf is not None:
                size = (sf.image.shape[1], sf.image.shape[0])
                view.pixmap = frame_pixmap(sf.image)
                view.frame_size = size
                with self.ctx.control._db_lock:
                    view.model = cal.model_for(self.ctx.conn, rec, frame_size=size)
                self._last_frame[rec.name] = time.time()
            lines = []
            if sf is not None and sf.has_angles:
                lines = [f"Кут місця: {sf.angles.elevation_deg:.2f}°",
                         f"Кут датчика: {wrap360(sf.angles.azimuth_deg):.2f}°", f"Нахил γ: {rec.tilt_deg:+.2f}°"]
            if st is not None and not st.connected:
                last = self._last_frame.get(rec.name)
                when = time.strftime("%H:%M:%S", time.localtime(last)) if last else "—"
                lines = [f"Немає звʼязку · останній кадр {when}"]
            view.text_lines = lines
            dc = self.ctx.detection.camera(rec.name)
            view.boxes = ([(o.u, o.v, o.w, o.h) for o in dc.result.objects]
                          if (self.ctx.detection.active and dc.result) else [])
            view.update()
