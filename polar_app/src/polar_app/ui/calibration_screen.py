"""«Камери / Калібрування»: перегляд камери з центром, колами поля зору і віссю нахилу; панорама з фігурою
поля зору; перевірка похибки розміщення за орієнтиром."""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                             QVBoxLayout, QWidget)

from polar_core import calibration as cal
from polar_core import cameras as cm
from polar_core.angles import wrap360
from polar_core.control import ManualControlLocked
from polar_core.projection import CameraModel, split_wrapped

from .common import BAD, GOOD, QUIET, WARN, frame_pixmap

FRACTIONS = (0.3, 0.5, 0.7, 1.0)
CENTER_COLOR = QColor("#ff3b30")
CIRCLE_COLOR = QColor("#ffd60a")
AXIS_COLOR = QColor("#30d158")
LANDMARK_COLOR = QColor("#64d2ff")


class CameraView(QWidget):
    """Живий кадр з накладками. Координати накладок — у пікселях кадру."""
    center_moved = pyqtSignal(float, float)
    clicked = pyqtSignal(float, float)          # клік у режимі орієнтира (пікселі кадру)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.pixmap: Optional[QPixmap] = None
        self.frame_size: Tuple[int, int] = (1920, 1080)
        self.model: Optional[CameraModel] = None
        self.hfov = 60.0
        self.landmark_mode = False
        self.landmarks: List[Tuple[float, float]] = []
        self.text_lines: List[str] = []
        self._drag = False

    # --- перетворення кадр ↔ віджет --------------------------------------------------
    def _rect(self) -> QRectF:
        w, h = self.frame_size
        s = min(self.width() / w, self.height() / h)
        return QRectF((self.width() - w * s) / 2, (self.height() - h * s) / 2, w * s, h * s)

    def to_widget(self, u: float, v: float) -> QPointF:
        r = self._rect()
        return QPointF(r.x() + u * r.width() / self.frame_size[0], r.y() + v * r.height() / self.frame_size[1])

    def to_frame(self, p: QPointF) -> Tuple[float, float]:
        r = self._rect()
        u = (p.x() - r.x()) * self.frame_size[0] / r.width()
        v = (p.y() - r.y()) * self.frame_size[1] / r.height()
        return min(max(u, 0.0), self.frame_size[0]), min(max(v, 0.0), self.frame_size[1])

    # --- мишка --------------------------------------------------------------------------
    def _near_center(self, p: QPointF) -> bool:
        if self.model is None:
            return False
        c = self.to_widget(self.model.cx, self.model.cy)
        return math.hypot(p.x() - c.x(), p.y() - c.y()) <= 18

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton or self.model is None:
            return
        p = e.position()
        if self.landmark_mode and not self._near_center(p):
            self.clicked.emit(*self.to_frame(p))
            return
        self._drag = True
        self._move_center(p)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag:
            self._move_center(e.position())
        else:
            self.setCursor(Qt.CursorShape.SizeAllCursor if self._near_center(e.position())
                           else (Qt.CursorShape.CrossCursor if self.landmark_mode else Qt.CursorShape.ArrowCursor))

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._drag = False

    def _move_center(self, p: QPointF) -> None:
        u, v = self.to_frame(p)
        self.center_moved.emit(u, v)

    # --- малювання ------------------------------------------------------------------------
    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#202020"))
        r = self._rect()
        if self.pixmap is not None:
            p.drawPixmap(r, self.pixmap, QRectF(self.pixmap.rect()))
        else:
            p.setPen(QColor("#bbbbbb"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Немає кадру")
        if self.model is None:
            p.end()
            return
        m = self.model
        p.setClipRect(r)
        # середина кадру — сірий хрестик
        mid = self.to_widget(m.width / 2, m.height / 2)
        p.setPen(QPen(QColor(170, 170, 170, 200), 1))
        p.drawLine(QPointF(mid.x() - 8, mid.y()), QPointF(mid.x() + 8, mid.y()))
        p.drawLine(QPointF(mid.x(), mid.y() - 8), QPointF(mid.x(), mid.y() + 8))
        c = self.to_widget(m.cx, m.cy)
        # вертикаль для порівняння і вісь нахилу γ (за годинниковою стрілкою від вертикалі)
        p.setPen(QPen(QColor(255, 255, 255, 110), 1))
        p.drawLine(QPointF(c.x(), r.top()), QPointF(c.x(), r.bottom()))
        g = math.radians(m.tilt_deg)
        L = max(r.width(), r.height())
        dx, dy = math.sin(g) * L, -math.cos(g) * L
        p.setPen(QPen(AXIS_COLOR, 2))
        p.drawLine(QPointF(c.x() - dx, c.y() - dy), QPointF(c.x() + dx, c.y() + dy))
        # кола поля зору з урахуванням дисторсії
        font = QFont(self.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1))
        p.setFont(font)
        for frac in FRACTIONS:
            pts = m.fov_circle(frac)
            poly = QPolygonF([self.to_widget(u, v) for u, v in pts])
            p.setPen(QPen(CIRCLE_COLOR, 1.5 if frac == 1.0 else 1))
            p.drawPolyline(poly)
            # підпис — на правому перетині кола з горизонталлю через центр: він завжди в кадрі
            # (вгорі великі кола виходять за кадр, бо VFOV < HFOV)
            lab = self.to_widget(pts[0][0], pts[0][1])
            p.setPen(CIRCLE_COLOR)
            angle = CameraModel.circle_angle(frac, m.hfov_deg)
            x = min(lab.x() - 46, r.right() - 50)
            p.drawText(QPointF(x, lab.y() - 18), f"{int(frac * 100)} %")
            p.drawText(QPointF(x, lab.y() - 5), f"{angle:.1f}°")
        # орієнтири
        p.setPen(QPen(LANDMARK_COLOR, 2))
        for u, v in self.landmarks:
            q = self.to_widget(u, v)
            p.drawEllipse(q, 6, 6)
        # центр камери — перехрестя
        p.setPen(QPen(CENTER_COLOR, 2))
        p.drawLine(QPointF(c.x() - 16, c.y()), QPointF(c.x() + 16, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - 16), QPointF(c.x(), c.y() + 16))
        p.drawEllipse(c, 5, 5)
        p.drawText(QPointF(c.x() + 10, c.y() - 10), f"X {m.cx:.0f}, Y {m.cy:.0f}")
        # кути від датчика поверх зображення
        p.setClipping(False)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        box_h = 16 * len(self.text_lines) + 8
        p.drawRect(QRectF(r.left() + 6, r.top() + 6, 300, box_h))
        p.setPen(QColor("white"))
        for i, line in enumerate(self.text_lines):
            p.drawText(QPointF(r.left() + 12, r.top() + 22 + 16 * i), line)
        p.end()


class PanoramaView(QWidget):
    """Панорама 360°×90° (поки — сітка) з фігурою поля зору, центром камери і лінією азимуту."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.pixmap: Optional[QPixmap] = None
        self.outline: Optional[np.ndarray] = None     # (азимут від півночі, кут місця)
        self.center: Optional[Tuple[float, float]] = None
        self.azimuth: Optional[float] = None
        self.label = ""

    def heightForWidth(self, w: int) -> int:  # noqa: N802
        return int(w / 4) + 24

    def _area(self) -> QRectF:
        w = self.width() - 40
        h = min(self.height() - 26, w / 4)
        return QRectF(30, 6, w, h)

    def _pt(self, az: float, el: float) -> QPointF:
        a = self._area()
        return QPointF(a.left() + az / 360.0 * a.width(), a.top() + (90.0 - el) / 90.0 * a.height())

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        a = self._area()
        p.fillRect(a, QColor("#1c1c1e"))
        if self.pixmap is not None:
            p.drawPixmap(a, self.pixmap, QRectF(self.pixmap.rect()))
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
        p.setFont(font)
        for az in range(0, 361, 30):
            x = self._pt(az, 0).x()
            p.setPen(QPen(QColor(255, 255, 255, 40), 1))
            p.drawLine(QPointF(x, a.top()), QPointF(x, a.bottom()))
            p.setPen(QColor(QUIET))
            p.drawText(QPointF(x - 8, a.bottom() + 14), f"{az}°")
        for el in range(0, 91, 15):
            y = self._pt(0, el).y()
            p.setPen(QPen(QColor(255, 255, 255, 40), 1))
            p.drawLine(QPointF(a.left(), y), QPointF(a.right(), y))
            p.setPen(QColor(QUIET))
            p.drawText(QPointF(2, y + 4), f"{el}°")
        p.setClipRect(a)
        if self.outline is not None and len(self.outline):
            pts = self.outline.copy()
            pts[:, 1] = np.minimum(pts[:, 1], 90.0)         # вище 90° — обрізається
            p.setPen(QPen(CIRCLE_COLOR, 1.5))
            p.setBrush(QColor(255, 214, 10, 40))
            for part in split_wrapped(pts):
                p.drawPolygon(QPolygonF([self._pt(az, el) for az, el in part]))
        if self.azimuth is not None:
            x = self._pt(self.azimuth, 0).x()
            p.setPen(QPen(QColor("#64d2ff"), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, a.top()), QPointF(x, a.bottom()))
            p.drawText(QPointF(min(x + 4, a.right() - 48), a.top() + 12), f"{self.azimuth:.1f}°")
        if self.center is not None:
            c = self._pt(*self.center)
            p.setPen(QPen(CENTER_COLOR, 2))
            p.drawLine(QPointF(c.x() - 6, c.y()), QPointF(c.x() + 6, c.y()))
            p.drawLine(QPointF(c.x(), c.y() - 6), QPointF(c.x(), c.y() + 6))
            if self.label:
                p.setPen(QColor("white"))
                p.drawText(QPointF(min(c.x() + 10, a.right() - 230), max(c.y() - 8, a.top() + 26)), self.label)
        p.end()


class CalibrationScreen(QWidget):
    closed = pyqtSignal()

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.rec = None
        self.calib: Optional[cal.Calibration] = None
        self._streaming: Optional[str] = None
        self._center_img: Optional[Tuple[float, float]] = None     # центр у пікселях кадру потоку
        self._img_size: Optional[Tuple[int, int]] = None
        self.check = cal.LandmarkCheck()

        v = QVBoxLayout(self)
        self.title = QLabel("Камери / Калібрування")
        self.title.setStyleSheet("font-size: 15px; font-weight: 600")
        v.addWidget(self.title)
        self.view = CameraView()
        self.view.center_moved.connect(self._on_center_moved)
        self.view.clicked.connect(self._on_landmark)
        v.addWidget(self.view, 1)

        row = QHBoxLayout()
        self.save_center_btn = QPushButton("Зберегти центр")
        self.save_center_btn.clicked.connect(self.save_center)
        self.reset_center_btn = QPushButton("Скинути центр")
        self.reset_center_btn.clicked.connect(self.reset_center)
        self.close_btn = QPushButton("Закрити")
        self.close_btn.clicked.connect(self.closed.emit)
        self.jog_btns = []
        row.addWidget(QLabel("Поворот:"))
        for text, da, de in (("◀", -1, 0), ("▶", 1, 0), ("▲", 0, 1), ("▼", 0, -1)):
            b = QPushButton(text)
            b.setFixedWidth(34)
            b.clicked.connect(lambda _=False, a=da, e=de: self._jog(a, e))
            row.addWidget(b)
            self.jog_btns.append(b)
        self.step = QDoubleSpinBox()
        self.step.setRange(0.1, 90)
        self.step.setValue(10.0)
        self.step.setSuffix("°")
        row.addWidget(self.step)
        row.addStretch(1)
        row.addWidget(QLabel("Нахил γ:"))
        self.tilt = QDoubleSpinBox()
        self.tilt.setRange(-180.0, 180.0)
        self.tilt.setDecimals(2)
        self.tilt.setSingleStep(0.1)
        self.tilt.setSuffix("°")
        self.tilt.setToolTip("Нахил камери відносно вертикалі, за годинниковою стрілкою — додатний. "
                             "Вісь нахилу на зображенні змінюється одразу; вертикальні обʼєкти мають іти паралельно їй.")
        self.tilt.valueChanged.connect(self._on_tilt)
        row.addWidget(self.tilt)
        self.save_tilt_btn = QPushButton("Зберегти нахил")
        self.save_tilt_btn.clicked.connect(self.save_tilt)
        row.addWidget(self.save_tilt_btn)
        row.addSpacing(12)
        row.addWidget(self.save_center_btn)
        row.addWidget(self.reset_center_btn)
        row.addWidget(self.close_btn)
        v.addLayout(row)

        acc = QGroupBox("Перевірка похибки розміщення")
        ah = QHBoxLayout(acc)
        self.landmark_btn = QPushButton("Позначати орієнтир")
        self.landmark_btn.setCheckable(True)
        self.landmark_btn.setToolTip("Клацніть по помітному нерухомому обʼєкту, поверніть камеру і клацніть "
                                     "по ньому ще раз. Похибка — розбіжність кутів орієнтира між позначками.")
        self.landmark_btn.toggled.connect(self._toggle_landmarks)
        self.clear_btn = QPushButton("Очистити")
        self.clear_btn.clicked.connect(self.clear_landmarks)
        self.save_error_btn = QPushButton("Записати похибку")
        self.save_error_btn.clicked.connect(self.save_error)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        ah.addWidget(self.landmark_btn)
        ah.addWidget(self.clear_btn)
        ah.addWidget(self.error_label, 1)
        ah.addWidget(self.save_error_btn)
        v.addWidget(acc)

        self.panorama = PanoramaView()
        v.addWidget(QLabel("Панорама камери (азимут від півночі 0–360°, кут місця 0–90°)"))
        v.addWidget(self.panorama)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        v.addWidget(self.message)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(100)

    # --- відкриття / вихід ------------------------------------------------------------
    def open_camera(self, camera_id: int) -> None:
        self.leave()
        self.rec = self.ctx.camera(camera_id)
        with self.ctx.control._db_lock:
            self.calib = cal.load(self.ctx.conn, camera_id)
            self.criterion = cal.accuracy_criterion(self.ctx.conn)
        self.title.setText(f"Камери / Калібрування · {self.rec.name}")
        self._loading_tilt = True
        self.tilt.setValue(self.rec.tilt_deg)
        self._loading_tilt = False
        self._tilt_value = self.rec.tilt_deg
        self._img_size = None
        self._center_img = None
        self.view.pixmap = None
        self.check.clear()
        self.view.landmarks = []
        self.landmark_btn.setChecked(False)
        self._say("Перетягніть червоне перехрестя в точку зображення, що відповідає кутам датчика.")
        self._show_error()
        self.save_tilt_btn.setEnabled(False)
        if self.ctx.video.acquire(self.rec.name):
            self._streaming = self.rec.name
        self.refresh()

    def leave(self) -> None:
        if self._streaming:
            self.ctx.video.release(self._streaming)
            self._streaming = None

    # --- модель і стан ---------------------------------------------------------------------
    def _size(self) -> Tuple[int, int]:
        return self._img_size or self.rec.frame_size

    def model(self) -> CameraModel:
        size = self._size()
        m = CameraModel.from_record(self.rec, frame_size=size)
        m.tilt_deg = getattr(self, "_tilt_value", self.rec.tilt_deg)     # значення з поля, ще не збережене
        if self._center_img is None:
            c = self.calib.center_for(size) if self.calib else None
            self._center_img = c or (size[0] / 2, size[1] / 2)
        m.cx, m.cy = self._center_img
        return m

    def refresh(self) -> None:
        if self.rec is None or not self.isVisible():
            return
        sf = self.ctx.video.latest(self.rec.name)
        if sf is not None:
            size = (sf.image.shape[1], sf.image.shape[0])
            if size != self._img_size:
                old = self._size()                  # до першого кадру — розмір із параметрів камери
                if self._center_img is not None:
                    self._center_img = (self._center_img[0] * size[0] / old[0],
                                        self._center_img[1] * size[1] / old[1])
                self._img_size = size
            self.view.pixmap = frame_pixmap(sf.image)
        m = self.model()
        self.view.frame_size = self._size()
        self.view.model = m
        st = self.ctx.control.status(self.rec.name)
        lines = [f"Центр камери: X {m.cx:.0f}, Y {m.cy:.0f} px",
                 f"Нахил γ: {m.tilt_deg:+.2f}°" + ("" if m.tilt_deg == self.rec.tilt_deg else " (не збережено)")]
        sensor = el = None
        if sf is not None and sf.has_angles:
            sensor, el = sf.angles.azimuth_deg, sf.angles.elevation_deg
        elif st.link_ok and st.azimuth_deg is not None:
            sensor, el = wrap360(st.azimuth_deg - self.rec.north_offset_deg), st.elevation_deg
        if sensor is not None:
            lines[0:0] = [f"Кут місця: {el:.2f}°", f"Кут датчика: {sensor:.2f}°"]
            self.panorama.outline = m.frame_outline(sensor, el)
            self.panorama.center = m.center_angles(sensor, el)
            self.panorama.azimuth = wrap360(sensor + self.rec.north_offset_deg)
            self.panorama.label = (f"{self.rec.fov_x_deg:g}°×{self.rec.fov_y_deg:g}° · датчик {sensor:.1f}° · "
                                   f"місце {el:.1f}°")
        else:
            lines.insert(0, "Кути датчика: немає звʼязку з сервоприводами")
            self.panorama.outline = None
            self.panorama.center = None
            self.panorama.azimuth = None
        self.view.text_lines = lines
        for b in self.jog_btns:
            b.setEnabled(st.link_ok)
        self.view.update()
        self.panorama.update()

    # --- центр -------------------------------------------------------------------------------
    def _on_center_moved(self, u: float, v: float) -> None:
        self._center_img = (u, v)
        self.view.landmarks = []
        if self.check.observations:
            self.check.clear()
            self._show_error()
        self.refresh()

    def save_center(self) -> None:
        m = self.model()
        size = self._size()
        with self.ctx.control._db_lock:
            cal.save_center(self.ctx.conn, self.rec.id, m.cx, m.cy, size)
            self.calib = cal.load(self.ctx.conn, self.rec.id)
        self._say(f"Центр збережено: X {m.cx:.0f}, Y {m.cy:.0f} px (кадр {size[0]}×{size[1]}).", GOOD)

    def reset_center(self) -> None:
        size = self._size()
        self._on_center_moved(size[0] / 2, size[1] / 2)
        self._say("Центр повернуто на середину кадру; натисніть «Зберегти центр», щоб записати.")

    # --- нахил ------------------------------------------------------------------------------------
    def _on_tilt(self, value: float) -> None:
        if getattr(self, "_loading_tilt", False) or self.rec is None:
            return
        self._tilt_value = value
        self.save_tilt_btn.setEnabled(value != self.rec.tilt_deg)
        if self.check.observations:              # позначки орієнтира зроблено з іншим нахилом
            self.clear_landmarks()
        self.refresh()

    def save_tilt(self) -> None:
        value = self.tilt.value()
        with self.ctx.control._db_lock:
            cm.set_tilt(self.ctx.conn, self.rec.id, value)
        self.rec = self.ctx.camera(self.rec.id)
        self.save_tilt_btn.setEnabled(False)
        self._say(f"Нахил γ = {value:+.2f}° збережено (те саме поле, що в «Редагуванні»).", GOOD)
        self.refresh()

    # --- орієнтир -----------------------------------------------------------------------------
    def _toggle_landmarks(self, on: bool) -> None:
        self.view.landmark_mode = on
        if on:
            self._say("Клацніть по орієнтиру; потім поверніть камеру (◀ ▶ ▲ ▼) і клацніть по ньому знову.")

    def _on_landmark(self, u: float, v: float) -> None:
        sf = self.ctx.video.latest(self.rec.name)
        if sf is None or not sf.has_angles:
            self._say("Немає кутів кадру — позначка не врахована (немає звʼязку з сервоприводами).", BAD)
            return
        if sf.angles.moving:
            self._say("Камера рухається — дочекайтесь зупинки і позначте орієнтир знову.", WARN)
            return
        az, el = self.model().pixel_to_angles(u, v, sf.angles.azimuth_deg, sf.angles.elevation_deg)
        self.check.add(az, el)
        self.view.landmarks = [(u, v)]
        self._show_error()

    def add_landmark_observation(self, az: float, el: float) -> None:
        self.check.add(az, el)
        self._show_error()

    def clear_landmarks(self) -> None:
        self.check.clear()
        self.view.landmarks = []
        self._show_error()

    def _show_error(self) -> None:
        n = len(self.check.observations)
        err = self.check.error()
        saved = self.calib.placement_error_deg if self.calib else None
        tail = f" · записана: {saved:.2f}°" if saved is not None else ""
        if err is None:
            self.error_label.setText(f"Позначок: {n} (потрібно щонайменше 2){tail}")
            self.error_label.setStyleSheet("")
            self.save_error_btn.setEnabled(False)
            return
        ok = err <= self.criterion
        self.error_label.setText(f"Позначок: {n} · похибка розміщення {err:.2f}° — "
                                 f"{'в межах' if ok else 'більша за'} критерій {self.criterion:g}°{tail}")
        self.error_label.setStyleSheet(f"color: {GOOD if ok else BAD}")
        self.save_error_btn.setEnabled(True)

    def save_error(self) -> None:
        err = self.check.error()
        if err is None:
            return
        with self.ctx.control._db_lock:
            cal.save_placement_error(self.ctx.conn, self.rec.id, err, self._size())
            self.calib = cal.load(self.ctx.conn, self.rec.id)
        self._show_error()
        self._say(f"Похибку розміщення {err:.2f}° записано в калібрування камери.", GOOD)

    # --- поворот камери ------------------------------------------------------------------------
    def _jog(self, d_az: int, d_el: int) -> None:
        try:
            self.ctx.control.jog(self.rec.name, d_az * self.step.value(), d_el * self.step.value(), 0.0)
        except (ManualControlLocked, RuntimeError) as e:
            self._say(str(e), BAD)

    def _say(self, text: str, color: str = "") -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {color}" if color else "")
