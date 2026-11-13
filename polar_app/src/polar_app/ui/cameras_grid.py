"""«Робота / Обʼєкти управління»: зображення з усіх камер одночасно з позначками калібрування і знайденими обʼєктами.

Під кожною камерою — її панорама з фігурою поля зору, кнопки кроку камери (▲ ▼ — на VFOV/2, ◀ ▶ — на
HFOV/2, по вузлах сітки Навчання) і «Зберегти»: поточний кадр нерухомої камери записується в панораму і
в знімки камери. «Знімок» відкриває окреме вікно зі збереженим знімком для поточного положення камери.
"""
from __future__ import annotations

import math
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from polar_core import calibration as cal
from polar_core.angles import wrap360
from polar_core.control import ManualControlLocked

from .calibration_screen import CameraView, PanoramaView
from .common import BAD, GOOD, frame_pixmap
from .snapshot_window import SnapshotWindow


class CameraTile(QWidget):
    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self.name = name
        self.pano_key = None
        self.pano_coverage = None
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)
        self.title = QLabel(name)
        self.title.setStyleSheet("font-weight: 600")
        v.addWidget(self.title)
        self.view = CameraView()
        self.view.interactive = False           # лише відображення: без перетягування центру
        self.view.setMinimumSize(320, 180)
        v.addWidget(self.view, 1)
        self.panorama = PanoramaView()          # без інструмента маски — лише показ
        self.panorama.setMinimumHeight(110)
        v.addWidget(self.panorama)
        row = QHBoxLayout()
        self.btn_left = QPushButton("◀")
        self.btn_up = QPushButton("▲")
        self.btn_down = QPushButton("▼")
        self.btn_right = QPushButton("▶")
        self.btn_left.setToolTip("Ліворуч на пів горизонтального кута огляду (HFOV/2)")
        self.btn_right.setToolTip("Праворуч на пів горизонтального кута огляду (HFOV/2)")
        self.btn_up.setToolTip("Вгору на пів вертикального кута огляду (VFOV/2)")
        self.btn_down.setToolTip("Вниз на пів вертикального кута огляду (VFOV/2)")
        self.step_btns = [self.btn_left, self.btn_up, self.btn_down, self.btn_right]
        for b in self.step_btns:
            b.setFixedWidth(36)
            row.addWidget(b)
        self.save_btn = QPushButton("Зберегти")
        self.save_btn.setToolTip("Записати поточний кадр нерухомої камери в панораму і в знімки камери")
        row.addWidget(self.save_btn)
        self.snap_btn = QPushButton("Знімок")
        self.snap_btn.setToolTip("Окреме вікно зі збереженим знімком для поточного положення камери")
        row.addWidget(self.snap_btn)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        row.addWidget(self.message, 1)
        v.addLayout(row)

    def say(self, text: str, color: str = "") -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {color}" if color else "")


class CamerasGridScreen(QWidget):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.tiles = {}
        self._held = []
        self._last_frame = {}
        self.snapshot_windows = {}
        v = QVBoxLayout(self)
        title = QLabel("Робота / Обʼєкти управління")
        title.setStyleSheet("font-size: 15px; font-weight: 600")
        v.addWidget(title)
        body = QWidget()
        self.grid = QGridLayout(body)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)
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
            tile.btn_left.clicked.connect(lambda _=False, nm=rec.name: self.step(nm, -1, 0))
            tile.btn_right.clicked.connect(lambda _=False, nm=rec.name: self.step(nm, +1, 0))
            tile.btn_up.clicked.connect(lambda _=False, nm=rec.name: self.step(nm, 0, +1))
            tile.btn_down.clicked.connect(lambda _=False, nm=rec.name: self.step(nm, 0, -1))
            tile.save_btn.clicked.connect(lambda _=False, nm=rec.name: self.save(nm))
            tile.snap_btn.clicked.connect(lambda _=False, nm=rec.name: self.open_snapshot(nm))
            self.tiles[rec.name] = tile
            self.grid.addWidget(tile, i // cols, i % cols)
            if self.ctx.video.acquire(rec.name):
                self._held.append(rec.name)
        self.refresh()

    def leave(self) -> None:
        for name in self._held:
            self.ctx.video.release(name)
        self._held = []

    # --- кнопки ---------------------------------------------------------------------------------
    def step(self, name: str, d_az: int, d_el: int) -> None:
        tile = self.tiles.get(name)
        try:
            az, el = self.ctx.learning.step(name, d_az, d_el)
        except (ManualControlLocked, RuntimeError, KeyError) as e:
            if tile:
                tile.say(str(e), BAD)
            return
        if tile:
            tile.say(f"→ азимут {az:.1f}°, кут місця {el:.1f}°")

    def open_snapshot(self, name: str) -> SnapshotWindow:
        w = self.snapshot_windows.get(name)
        if w is None:
            w = SnapshotWindow(self.ctx, name)
            self.snapshot_windows[name] = w
        else:
            w.timer.start(250)
            w.refresh()
        w.show()
        w.raise_()
        w.activateWindow()
        return w

    def save(self, name: str) -> None:
        tile = self.tiles.get(name)
        try:
            cov, path = self.ctx.learning.save_current(name)
        except (RuntimeError, KeyError, OSError, ValueError) as e:
            if tile:
                tile.say(str(e), BAD)
            return
        if tile:
            tile.say(f"Збережено: панорама (покриття {cov * 100:.1f} %) і знімок …/{_tail(path)}", GOOD)
            tile.setToolTip(path)

    # --- оновлення --------------------------------------------------------------------------------
    def refresh(self) -> None:
        if not self.isVisible():
            return
        learning_on = self.ctx.learning.is_running()
        for rec in self.ctx.records():
            tile = self.tiles.get(rec.name)
            if tile is None:
                continue
            view = tile.view
            sf = self.ctx.video.latest(rec.name)
            st = self.ctx.video.stats(rec.name)
            model = None
            if sf is not None:
                size = (sf.image.shape[1], sf.image.shape[0])
                view.pixmap = frame_pixmap(sf.image)
                view.frame_size = size
                with self.ctx.control._db_lock:
                    model = cal.model_for(self.ctx.conn, rec, frame_size=size)
                view.model = model
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
            self._refresh_panorama(tile, rec, sf, model)
            link = self.ctx.control.status(rec.name).link_ok
            busy = self.ctx.learning.is_running(rec.name)
            for b in tile.step_btns:
                b.setEnabled(link and not busy)
            tile.save_btn.setEnabled(sf is not None and not learning_on)

    def _refresh_panorama(self, tile: CameraTile, rec, sf, model) -> None:
        pv = tile.panorama
        learning = self.ctx.learning
        pano = learning.panorama(rec.name)
        key = (id(pano), learning.version.get(rec.name, 0))
        if key != tile.pano_key:
            tile.pano_key = key
            tile.pano_coverage = None if pano is None else pano.coverage()
            if pano is None:
                pv.pixmap = None
            else:
                import cv2

                w = max(360, min(pano.width, 1440))
                pv.pixmap = frame_pixmap(cv2.resize(pano.image, (w, int(w / 4)), interpolation=cv2.INTER_AREA))
        if model is not None and sf is not None and sf.has_angles:
            sensor, el = sf.angles.azimuth_deg, sf.angles.elevation_deg
            pv.outline = model.frame_outline(sensor, el)
            pv.center = model.center_angles(sensor, el)
            pv.azimuth = wrap360(sensor + rec.north_offset_deg)
        else:
            pv.outline = pv.center = pv.azimuth = None
        cov = (f"покриття {tile.pano_coverage * 100:.1f} %" if tile.pano_coverage is not None
               else "панорами ще немає")
        pv.label = f"азимут {pv.azimuth:.1f}° · місце {sf.angles.elevation_deg:.1f}°" if pv.azimuth is not None else ""
        tile.title.setText(f"{rec.name} · панорама: {cov}")
        pv.update()


def _tail(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-2:])
