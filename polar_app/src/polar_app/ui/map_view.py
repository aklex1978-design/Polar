"""Віджет карти: на кожному кроці зуму — карта свого рівня у власному масштабі файлу.

«+» / «−» і колесо перемикають завантажені рівні від оглядового до найдетальнішого (за роздільною
здатністю). Під картою поточного рівня малюються дрібніші рівні — там, де карти поточного рівня немає,
видно збільшену дрібнішу. Без завантажених карт — сітка на сірому тлі, камери в своїх координатах.
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
# без карт — кроки зуму вебкарти 10…17
EMPTY_STEPS = [mp.res_at_zoom(z) for z in range(10, 18)]


class MapView(QWidget):
    level_changed = pyqtSignal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 300)
        self.setMouseTracking(True)
        self.levels: List[mp.MapLevel] = []         # від оглядового (більший крок) до детального
        self.step = 0
        self._empty_step = 4
        self.cx, self.cy = mp.lonlat_to_merc(30.52, 50.45)      # центр вигляду, EPSG:3857
        self._drag: Optional[QPointF] = None
        self._cache: "OrderedDict[str, Optional[QImage]]" = OrderedDict()
        self.overlays: List[Callable[[QPainter, "MapView"], None]] = []
        self.attribution = ""

    # --- дані -------------------------------------------------------------------------------------------
    def set_levels(self, levels: List[mp.MapLevel]) -> None:
        current = self.current
        self.levels = sorted(levels, key=lambda lv: -lv.res)
        self._cache.clear()
        self.attribution = next((lv.attribution for lv in self.levels if lv.attribution), "")
        if self.levels:
            if current is not None and any(lv.level == current.level for lv in self.levels):
                self.step = next(i for i, lv in enumerate(self.levels) if lv.level == current.level)
            else:
                self.step = self._best_step()
        self.level_changed.emit(self.step)
        self.update()

    def _best_step(self) -> int:
        """Найдетальніший рівень, що покриває центр вигляду; інакше — оглядовий."""
        for i in range(len(self.levels) - 1, -1, -1):
            a, b, c, d = self.levels[i].bounds
            if a <= self.cx <= c and b <= self.cy <= d:
                return i
        return 0

    def center_on(self, lat: float, lon: float) -> None:
        self.cx, self.cy = mp.lonlat_to_merc(lon, lat)
        self.update()

    @property
    def current(self) -> Optional[mp.MapLevel]:
        return self.levels[self.step] if self.levels and 0 <= self.step < len(self.levels) else None

    @property
    def steps(self) -> int:
        return len(self.levels) if self.levels else len(EMPTY_STEPS)

    @property
    def index(self) -> int:
        return self.step if self.levels else self._empty_step

    @property
    def res(self) -> float:
        cur = self.current
        return cur.res if cur else EMPTY_STEPS[self._empty_step]

    def set_index(self, i: int) -> None:
        i = max(0, min(self.steps - 1, i))
        if i != self.index:
            if self.levels:
                self.step = i
            else:
                self._empty_step = i
            self.level_changed.emit(i)
            self.update()

    def zoom_in(self) -> None:
        self.set_index(self.index + 1)

    def zoom_out(self) -> None:
        self.set_index(self.index - 1)

    def can_zoom_in(self) -> bool:
        return self.index < self.steps - 1

    def can_zoom_out(self) -> bool:
        return self.index > 0

    @property
    def level_text(self) -> str:
        cur = self.current
        if cur is None:
            return f"Карт немає · масштаб {self.index + 1} з {self.steps}"
        return (f"Рівень {cur.level} · {os.path.basename(cur.source_file)} · {cur.ground_res():.2f} м/піксель "
                f"({self.index + 1} з {self.steps})")

    # --- координати ------------------------------------------------------------------------------------------
    def to_screen(self, lat: float, lon: float) -> QPointF:
        x, y = mp.lonlat_to_merc(lon, lat)
        r = self.res
        return QPointF(self.width() / 2 + (x - self.cx) / r, self.height() / 2 - (y - self.cy) / r)

    def to_lonlat(self, p: QPointF) -> Tuple[float, float]:
        r = self.res
        return mp.merc_to_lonlat(self.cx + (p.x() - self.width() / 2) * r, self.cy - (p.y() - self.height() / 2) * r)

    # --- миша ------------------------------------------------------------------------------------------------
    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.position()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None:
            d = e.position() - self._drag
            self._drag = e.position()
            r = self.res
            self.cx -= d.x() * r
            self.cy += d.y() * r
            self.update()

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self._drag = None

    def wheelEvent(self, e) -> None:  # noqa: N802
        steps = e.angleDelta().y()
        if steps:
            p = e.position()                     # точка під курсором лишається на місці
            r0 = self.res
            mx, my = self.cx + (p.x() - self.width() / 2) * r0, self.cy - (p.y() - self.height() / 2) * r0
            self.set_index(self.index + (1 if steps > 0 else -1))
            r1 = self.res
            self.cx, self.cy = mx - (p.x() - self.width() / 2) * r1, my + (p.y() - self.height() / 2) * r1
            self.update()

    # --- тайли ---------------------------------------------------------------------------------------------------
    def _tile(self, lv: mp.MapLevel, tx: int, ty: int) -> Optional[QImage]:
        key = f"{lv.level}/{lv.loaded_at}/{tx}/{ty}"
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        path = lv.tile_path(tx, ty)
        img = QImage(path) if os.path.isfile(path) else None
        if img is not None and img.isNull():
            img = None
        self._cache[key] = img
        while len(self._cache) > 800:
            self._cache.popitem(last=False)
        return img

    def _draw_level(self, p: QPainter, lv: mp.MapLevel) -> int:
        """Тайли рівня, що потрапляють у вигляд, у масштабі поточного рівня. Повертає кількість."""
        r = self.res
        k = lv.res / r                                  # пікселів екрана на піксель рівня
        size = TILE * k
        left = self.cx - self.width() / 2 * r            # межі вигляду в EPSG:3857
        top = self.cy + self.height() / 2 * r
        tx0 = max(0, int(math.floor((left - lv.x0) / (lv.res * TILE))))
        ty0 = max(0, int(math.floor((lv.y0 - top) / (lv.res * TILE))))
        tx1 = min(math.ceil(lv.width / TILE) - 1, int(math.floor((left + self.width() * r - lv.x0) / (lv.res * TILE))))
        ty1 = min(math.ceil(lv.height / TILE) - 1, int(math.floor((lv.y0 - (top - self.height() * r)) / (lv.res * TILE))))
        if tx1 < tx0 or ty1 < ty0 or (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > 2500:
            return 0
        n = 0
        ox = (lv.x0 - left) / r
        oy = (top - lv.y0) / r
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                img = self._tile(lv, tx, ty)
                if img is not None:
                    p.drawImage(QRectF(ox + tx * size, oy + ty * size, size, size), img)
                    n += 1
        return n

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.fillRect(self.rect(), QColor("#d9d9d4"))
        drawn = 0
        if self.levels:
            # спершу дрібніші рівні (запас там, де карти поточного немає), зверху — поточний
            for lv in self.levels[: self.step + 1]:
                drawn += self._draw_level(p, lv)
        if not drawn:                                  # немає карти у вигляді — сітка 1 км
            self._draw_grid(p)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for draw in self.overlays:
            draw(p, self)
        f = QFont(p.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        p.setFont(f)
        if self.attribution:
            w = p.fontMetrics().horizontalAdvance(self.attribution) + 10
            box = QRectF(self.width() - w - 2, self.height() - 18, w, 16)
            p.fillRect(box, QColor(255, 255, 255, 200))
            p.setPen(QColor("#333"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, self.attribution)
        p.end()

    def _draw_grid(self, p: QPainter) -> None:
        r = self.res
        p.setPen(QPen(QColor("#c4c4be"), 1))
        step = 1000.0 / r
        while step < 40:
            step *= 2
        left = (self.cx / r - self.width() / 2)
        top = (-self.cy / r - self.height() / 2)
        x = (-left) % step
        while x < self.width():
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
            x += step
        y = (-top) % step
        while y < self.height():
            p.drawLine(QPointF(0, y), QPointF(self.width(), y))
            y += step
        p.setPen(QColor("#6b6b6b"))
        p.drawText(QPointF(10, 20), "Тут карти немає — завантажте рівні в «Налаштування / Карта»"
                   if self.levels else "Карту ще не завантажено («Налаштування / Карта»)")


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
