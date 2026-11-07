"""Віджет карти: тайли 8 збережених рівнів, зум «+» / «−» і колесом, перетягування мишею, позначки.

Рівень без власних тайлів (збільшений) або ділянка, де детального файлу немає, показується збільшеним
зображенням найближчого дрібнішого рівня. Без завантаженої карти — сітка на сірому тлі, камери все
одно стоять у своїх координатах.
"""
from __future__ import annotations

import math
import os
from collections import OrderedDict
from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from polar_core import maps as mp

TILE = mp.TILE


class MapView(QWidget):
    level_changed = pyqtSignal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 300)
        self.setMouseTracking(True)
        self.levels: List[mp.MapLevel] = []
        self.base_zoom = 14
        self.level = 1                          # 1…8
        self.cx, self.cy = mp.lonlat_to_merc(30.52, 50.45)      # центр вигляду, EPSG:3857
        self._drag: Optional[QPointF] = None
        self._cache: "OrderedDict[str, Optional[QImage]]" = OrderedDict()
        self.overlays: List[Callable[[QPainter, "MapView"], None]] = []
        self.attribution = ""

    # --- дані -------------------------------------------------------------------------------------------
    def set_levels(self, levels: List[mp.MapLevel], base_zoom: Optional[int] = None) -> None:
        self.levels = levels
        self._cache.clear()
        if levels:
            self.base_zoom = levels[0].zoom
        elif base_zoom is not None:
            self.base_zoom = base_zoom
        self.attribution = next((lv.attribution for lv in levels if lv.attribution), "")
        self.update()

    def center_on(self, lat: float, lon: float) -> None:
        self.cx, self.cy = mp.lonlat_to_merc(lon, lat)
        self.update()

    @property
    def zoom(self) -> int:
        return self.base_zoom + self.level - 1

    def set_level(self, level: int) -> None:
        level = max(1, min(mp.LEVELS, level))
        if level != self.level:
            self.level = level
            self.level_changed.emit(level)
            self.update()

    def zoom_in(self) -> None:
        self.set_level(self.level + 1)

    def zoom_out(self) -> None:
        self.set_level(self.level - 1)

    @property
    def level_text(self) -> str:
        lv = next((x for x in self.levels if x.level == self.level), None)
        tail = f" · {lv.warning}" if lv and lv.state == "upsampled" else ""
        return f"Рівень {self.level} з {mp.LEVELS} (зум {self.zoom}){tail}"

    # --- координати ------------------------------------------------------------------------------------------
    def _res(self) -> float:
        return mp.res_at_zoom(self.zoom)

    def to_screen(self, lat: float, lon: float) -> QPointF:
        x, y = mp.lonlat_to_merc(lon, lat)
        r = self._res()
        return QPointF(self.width() / 2 + (x - self.cx) / r, self.height() / 2 - (y - self.cy) / r)

    def to_lonlat(self, p: QPointF) -> Tuple[float, float]:
        r = self._res()
        return mp.merc_to_lonlat(self.cx + (p.x() - self.width() / 2) * r, self.cy - (p.y() - self.height() / 2) * r)

    # --- миша ------------------------------------------------------------------------------------------------
    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.position()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None:
            d = e.position() - self._drag
            self._drag = e.position()
            r = self._res()
            self.cx -= d.x() * r
            self.cy += d.y() * r
            self.update()

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self._drag = None

    def wheelEvent(self, e) -> None:  # noqa: N802
        steps = e.angleDelta().y()
        if steps:
            # точка під курсором лишається на місці
            p = e.position()
            r0 = self._res()
            mx, my = self.cx + (p.x() - self.width() / 2) * r0, self.cy - (p.y() - self.height() / 2) * r0
            self.set_level(self.level + (1 if steps > 0 else -1))
            r1 = self._res()
            self.cx, self.cy = mx - (p.x() - self.width() / 2) * r1, my + (p.y() - self.height() / 2) * r1
            self.update()

    # --- тайли ---------------------------------------------------------------------------------------------------
    def _tile(self, level: int, tx: int, ty: int) -> Optional[QImage]:
        lv = next((x for x in self.levels if x.level == level), None)
        if lv is None or lv.state not in ("file", "downsampled"):
            return None
        key = f"{level}/{tx}/{ty}"
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        path = os.path.join(lv.tiles_dir, str(tx), f"{ty}.png")
        img = QImage(path) if os.path.isfile(path) else None
        if img is not None and img.isNull():
            img = None
        self._cache[key] = img
        while len(self._cache) > 600:
            self._cache.popitem(last=False)
        return img

    def _draw_tile(self, p: QPainter, tx: int, ty: int, dst: QRectF) -> bool:
        """Тайл рівня, а де його немає — збільшена частина тайла найближчого дрібнішого рівня."""
        for level in range(self.level, 0, -1):
            d = self.level - level
            img = self._tile(level, tx >> d, ty >> d)
            if img is None:
                continue
            k = 1 << d
            size = TILE / k
            src = QRectF((tx % k) * size, (ty % k) * size, size, size)
            p.drawImage(dst, img, src)
            return True
        return False

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.fillRect(self.rect(), QColor("#d9d9d4"))
        z = self.zoom
        gcx, gcy = mp.global_px(self.cx, self.cy, z)
        left, top = gcx - self.width() / 2, gcy - self.height() / 2
        tx0, ty0 = int(math.floor(left / TILE)), int(math.floor(top / TILE))
        tx1, ty1 = int(math.floor((left + self.width()) / TILE)), int(math.floor((top + self.height()) / TILE))
        n = 1 << z
        drawn = 0
        if self.levels:
            for tx in range(tx0, tx1 + 1):
                for ty in range(max(0, ty0), min(n - 1, ty1) + 1):
                    dst = QRectF(tx * TILE - left, ty * TILE - top, TILE, TILE)
                    drawn += self._draw_tile(p, tx % n, ty, dst)
        if not drawn:                                  # немає карти — сітка 1 км
            p.setPen(QPen(QColor("#c4c4be"), 1))
            r = self._res()
            step = 1000.0 / r
            while step < 40:
                step *= 2
            x = (-left) % step
            while x < self.width():
                p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
                x += step
            y = (-top) % step
            while y < self.height():
                p.drawLine(QPointF(0, y), QPointF(self.width(), y))
                y += step
            p.setPen(QColor("#6b6b6b"))
            p.drawText(QPointF(10, 20), "Карту ще не завантажено («Налаштування / Карта»)")
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for draw in self.overlays:
            draw(p, self)
        # підпис джерела і рівень
        f = QFont(p.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        p.setFont(f)
        if self.attribution:
            w = p.fontMetrics().horizontalAdvance(self.attribution) + 10
            box = QRectF(self.width() - w - 2, self.height() - 18, w, 16)
            p.fillRect(box, QColor(255, 255, 255, 200))
            p.setPen(QColor("#333"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, self.attribution)
        text = self.level_text
        w = p.fontMetrics().horizontalAdvance(text) + 10
        box = QRectF(2, self.height() - 18, w, 16)
        p.fillRect(box, QColor(255, 255, 255, 200))
        p.setPen(QColor("#333"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        p.end()


def draw_camera_points(cameras: Callable[[], List[Tuple[str, float, float]]]):
    """Позначки камер з підписами (для «Налаштування / Карта»)."""
    def draw(p: QPainter, view: MapView) -> None:
        for name, lat, lon in cameras():
            pt = view.to_screen(lat, lon)
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor("#1e66d0"))
            p.drawEllipse(pt, 6, 6)
            p.setPen(QColor("#102a55"))
            p.drawText(pt + QPointF(9, -7), name)
    return draw
