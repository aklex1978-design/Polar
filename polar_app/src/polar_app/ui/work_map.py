"""«Робота / Карта»: та сама карта, що в «Налаштування / Карта», з трикутниками камер і цілями.

Трикутник: вершина — у GPS-точці камери, кут у вершині = HFOV, напрям — поточний азимут від півночі
(кут датчика + зміщення нуля), висота ≈ 70 пікселів на будь-якому рівні. Без датчика — останній
відомий азимут (або зміщення нуля, якщо камера ще не виходила на звʼязок).
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QTimer
from PyQt6.QtGui import QColor, QPen, QPolygonF
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .map_settings import MapToolbar
from .map_view import MapView

HEIGHT_PX = 70.0


def triangle(apex: QPointF, azimuth_deg: float, hfov_deg: float, height: float = HEIGHT_PX) -> QPolygonF:
    """Трикутник поля зору на екрані: вершина в apex, вісь — за азимутом (0° — вгору), кут — HFOV."""
    half = math.radians(min(hfov_deg, 170.0) / 2)
    side = height / math.cos(half)
    pts = [apex]
    for s in (-1, 1):
        a = math.radians(azimuth_deg) + s * half
        pts.append(QPointF(apex.x() + side * math.sin(a), apex.y() - side * math.cos(a)))
    return QPolygonF(pts)


class WorkMapScreen(QWidget):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._last_az = {}
        v = QVBoxLayout(self)
        title = QLabel("Робота / Карта")
        title.setStyleSheet("font-size: 15px; font-weight: 600")
        v.addWidget(title)
        self.view = MapView()
        self.view.overlays.append(self._draw_cameras)
        self.view.overlays.append(self._draw_targets)
        self.view.overlays.append(self._draw_north)
        v.addWidget(self.view, 1)
        self.toolbar = MapToolbar(self.view)
        self.toolbar.center_btn.clicked.connect(self.center)
        v.addWidget(self.toolbar)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(200)

    def enter(self) -> None:
        self.center()
        self.view.set_levels(self.ctx.maps.levels())
        self.toolbar.refresh()

    def center(self) -> None:
        c = self.ctx.maps.center()
        if c is not None:
            self.view.center_on(*c)

    def _tick(self) -> None:
        if self.isVisible():
            self.view.update()

    # --- шари ---------------------------------------------------------------------------------------------
    def azimuth(self, rec) -> float:
        st = self.ctx.control.status(rec.name)
        if st.link_ok and st.azimuth_deg is not None:
            self._last_az[rec.name] = st.azimuth_deg
        return self._last_az.get(rec.name, rec.north_offset_deg)

    def _draw_cameras(self, p, view: MapView) -> None:
        for rec in self.ctx.records():
            if rec.latitude is None or rec.longitude is None:
                continue
            apex = view.to_screen(rec.latitude, rec.longitude)
            online = self.ctx.control.status(rec.name).link_ok
            poly = triangle(apex, self.azimuth(rec), rec.fov_x_deg)
            fill = QColor(30, 102, 208, 90) if online else QColor(120, 120, 120, 80)
            p.setPen(QPen(QColor("#1e66d0") if online else QColor("#6b6b6b"), 2))
            p.setBrush(fill)
            p.drawPolygon(poly)
            p.setBrush(QColor("white"))
            p.drawEllipse(apex, 3.5, 3.5)
            p.setPen(QColor("#102a55"))
            p.drawText(apex + QPointF(8, 14), rec.name)

    def _draw_targets(self, p, view: MapView) -> None:
        det = self.ctx.detection
        if not det.active:
            return
        for tt in list(det.targets.targets.values()):
            g = tt.geo
            pt = view.to_screen(g.latitude, g.longitude)
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor("#c62828"))
            p.drawEllipse(pt, 7, 7)
            p.setPen(QColor("#7a1010"))
            p.drawText(pt + QPointF(10, -8), f"№{tt.number} · {g.altitude_m:.0f} м")

    def _draw_north(self, p, view: MapView) -> None:
        x, y = view.width() - 30, 40
        p.setPen(QPen(QColor("#333"), 2))
        p.setBrush(QColor("#333"))
        p.drawPolygon(QPolygonF([QPointF(x, y - 22), QPointF(x - 7, y - 4), QPointF(x + 7, y - 4)]))
        p.drawLine(QPointF(x, y - 4), QPointF(x, y + 10))
        p.drawText(QPointF(x - 9, y + 26), "Пн")
