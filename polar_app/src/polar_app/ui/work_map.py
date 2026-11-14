"""«Робота / Карта»: та сама карта, що в «Налаштування / Карта», з трикутниками камер і цілями.

Трикутник: вершина — у GPS-точці камери, кут у вершині = HFOV, напрям — поточний азимут від півночі
(кут датчика + зміщення нуля), висота ≈ 70 пікселів на будь-якому рівні. Без датчика — останній
відомий азимут (або зміщення нуля, якщо камера ще не виходила на звʼязок).

«Демо» — демонстрація визначення цілі: правий клік на карті ставить червону ціль, і кожна камера
повертається так, щоб її центр дивився на ціль (азимут і кут місця — точно, з координат камери й
цілі, з урахуванням кривини Землі). Висота цілі — поле поруч: «над камерами» (від середньої висоти
камер, за замовчуванням +100 м) або «над рівнем моря»; зміна висоти одразу перенацілює камери.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PyQt6.QtCore import QPointF, Qt, QTimer
from PyQt6.QtGui import QColor, QPen, QPolygonF
from PyQt6.QtWidgets import QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from polar_core.control import ManualControlLocked
from polar_core.geo import GeoPoint, LocalFrame, bearing_elevation

from .common import BAD

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
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        self.demo_btn = QPushButton("Демо")
        self.demo_btn.setCheckable(True)
        self.demo_btn.setToolTip("Демо визначення цілі: правий клік на карті ставить ціль, камери "
                                 "повертаються центром на неї")
        self.demo_btn.toggled.connect(self._demo_toggled)
        head.addWidget(self.demo_btn)
        head.addSpacing(12)
        head.addWidget(QLabel("Висота цілі, м"))
        self.demo_alt = QDoubleSpinBox()
        self.demo_alt.setRange(-1000.0, 20000.0)
        self.demo_alt.setDecimals(1)
        self.demo_alt.setSingleStep(10.0)
        self.demo_alt.setValue(100.0)
        self.demo_alt.setToolTip("Висота цілі: над середньою висотою камер або над рівнем моря")
        self.demo_alt.valueChanged.connect(self._demo_alt_changed)
        head.addWidget(self.demo_alt)
        self.demo_alt_mode = QComboBox()
        self.demo_alt_mode.addItems(["над камерами", "над рівнем моря"])
        self.demo_alt_mode.currentIndexChanged.connect(self._demo_mode_changed)
        head.addWidget(self.demo_alt_mode)
        v.addLayout(head)
        self.demo_target: Optional[GeoPoint] = None
        self.demo_aims: List[Tuple[str, float, float, float]] = []      # камера, азимут, кут місця, відстань
        self.view = MapView()
        self.view.right_clicked.connect(self.on_right_click)
        self.view.camera_points = lambda: [(la, lo) for _n, la, lo in self.ctx.maps.camera_points()]
        self.cameras = CameraTriangles(ctx)
        self.view.overlays.append(self.cameras)
        self.view.overlays.append(self._draw_targets)
        self.view.overlays.append(self._draw_demo)
        self.view.overlays.append(self._draw_north)
        v.addWidget(self.view, 1)
        self.toolbar = MapToolbar(self.view)
        self.toolbar.center_btn.clicked.connect(self.center)
        v.addWidget(self.toolbar)
        self.demo_info = QLabel("")
        self.demo_info.setWordWrap(True)
        v.addWidget(self.demo_info)
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

    # --- демо визначення цілі ------------------------------------------------------------------------------
    def _demo_toggled(self, on: bool) -> None:
        if on:
            self.demo_info.setText("Демо: клацніть правою кнопкою миші на карті — там зʼявиться ціль, "
                                   "і камери повернуться на неї.")
            self.demo_info.setStyleSheet("")
        else:
            self.demo_target = None
            self.demo_aims = []
            self.demo_info.setText("")
        self.view.update()

    def _cameras_alt(self) -> float:
        recs = [r for r in self.ctx.active_records() if r.latitude is not None and r.longitude is not None]
        return sum((r.altitude_m or 0.0) for r in recs) / len(recs) if recs else 0.0

    def target_altitude(self) -> float:
        """Висота цілі над рівнем моря з поля «Висота цілі»."""
        v = self.demo_alt.value()
        return v if self.demo_alt_mode.currentIndex() == 1 else self._cameras_alt() + v

    def _demo_mode_changed(self, index: int) -> None:
        """Перемикач способу відліку зберігає ту саму висоту цілі — змінюється лише число в полі."""
        base = self._cameras_alt()
        self.demo_alt.blockSignals(True)
        self.demo_alt.setValue(self.demo_alt.value() + base if index == 1 else self.demo_alt.value() - base)
        self.demo_alt.blockSignals(False)

    def _demo_alt_changed(self, _v: float) -> None:
        if self.demo_btn.isChecked() and self.demo_target is not None:
            self.set_demo_target(self.demo_target.latitude, self.demo_target.longitude)

    def on_right_click(self, lat: float, lon: float) -> None:
        if not self.demo_btn.isChecked():
            return
        self.set_demo_target(lat, lon)

    def set_demo_target(self, lat: float, lon: float) -> List[Tuple[str, float, float, float]]:
        """Ціль на середній висоті камер; кожна камера — центром на ціль. Повертає кути камер."""
        recs = [r for r in self.ctx.active_records() if r.latitude is not None and r.longitude is not None]
        if not recs:
            self.demo_info.setText("Демо: у камер немає координат.")
            self.demo_info.setStyleSheet(f"color: {BAD}")
            return []
        alt = self.target_altitude()
        target = GeoPoint(lat, lon, alt)
        frame = LocalFrame(target)
        aims, errors = [], []
        for r in recs:
            az, el, dist = bearing_elevation(frame, GeoPoint(r.latitude, r.longitude, r.altitude_m or 0.0), target)
            try:
                t_az, t_el = self.ctx.control.go_to(r.name, az, el, 0.0)
                aims.append((r.name, az, el, dist))
                if abs(t_el - el) > 0.05:                     # кут місця обмежено межами сервопривода
                    errors.append(f"{r.name}: кут місця {el:.2f}° обмежено до {t_el:.2f}°")
            except (ManualControlLocked, RuntimeError, KeyError, ValueError) as e:
                errors.append(f"{r.name}: {e}")
        self.demo_target, self.demo_aims = target, aims
        rel = alt - self._cameras_alt()
        lines = [f"Ціль: {lat:.6f}, {lon:.6f}, висота {alt:.1f} м над рівнем моря ({rel:+.1f} м над камерами)"]
        lines += [f"{n}: азимут {a:.2f}°, кут місця {e:+.2f}°, відстань {d:.0f} м" for n, a, e, d in aims]
        if errors:
            lines.append("Увага: " + "; ".join(errors))
        self.demo_info.setText("\n".join(lines))
        self.demo_info.setStyleSheet(f"color: {BAD}" if errors and not aims else "")
        self.view.update()
        return aims

    def _draw_demo(self, p, view: MapView) -> None:
        if not self.demo_btn.isChecked() or self.demo_target is None:
            return
        g = self.demo_target
        pt = view.to_screen(g.latitude, g.longitude)
        pen = QPen(QColor(198, 40, 40, 180), 1.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        for r in self.ctx.active_records():
            if r.latitude is not None and r.longitude is not None:
                p.drawLine(view.to_screen(r.latitude, r.longitude), pt)
        p.setPen(QPen(QColor("white"), 2))
        p.setBrush(QColor("#e53935"))
        p.drawEllipse(pt, 7, 7)
        p.setPen(QColor("#7a1010"))
        p.drawText(pt + QPointF(10, -8), f"ціль · {g.altitude_m:.0f} м")

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
