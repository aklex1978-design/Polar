"""«Робота / Карта»: та сама карта, що в «Налаштування / Карта», з трикутниками камер і цілями.

Трикутник: вершина — у GPS-точці камери, кут у вершині = HFOV, напрям — поточний азимут від півночі
(кут датчика + зміщення нуля), висота ≈ 70 пікселів на будь-якому рівні. Без датчика — останній
відомий азимут (або зміщення нуля, якщо камера ще не виходила на звʼязок).
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QTimer
from PyQt6.QtGui import QColor, QPen, QPolygonF
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .map_settings import MapToolbar
from .map_view import CameraTriangles, MapView, triangle  # noqa: F401 — triangle: для тестів

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
        self.view.camera_points = lambda: [(la, lo) for _n, la, lo in self.ctx.maps.camera_points()]
        self.cameras = CameraTriangles(ctx)
        self.view.overlays.append(self.cameras)
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
        self.view.recenter()

    def _tick(self) -> None:
        if self.isVisible():
            self.view.update()

    # --- шари ---------------------------------------------------------------------------------------------
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
