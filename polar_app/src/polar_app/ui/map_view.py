"""Віджет карти: на кожному кроці зуму — карта свого рівня у власному масштабі файлу.

Рівень 1 — найбільша деталізація, рівень 8 — найменша. «+» (і колесо від себе) веде до рівня 1,
«−» — до рівня 8. На кожному рівні карта показується цілком (як у переглядачі зображень): вона лише
зменшується, ніколи не збільшується відносно файлу, а для зменшення береться найближча з якісно
зменшених копій (p1, p2 …), тож дрібні підписи не губляться. Центр — середина між камерами в межах
карти (карта, менша за вигляд, — посередині). Карта рівня без жодної камери показується порожньою.
"""
from __future__ import annotations

import math
import os
from collections import OrderedDict
from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF
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
        self.levels: List[mp.MapLevel] = []         # від рівня 8 (найменш детальний) до рівня 1
        self.step = 0
        self._empty_step = 4
        self.cx, self.cy = mp.lonlat_to_merc(30.52, 50.45)      # центр вигляду, EPSG:3857
        self._drag: Optional[QPointF] = None
        self._cache: "OrderedDict[str, Optional[QImage]]" = OrderedDict()
        self._pyramid = {}
        self.overlays: List[Callable[[QPainter, "MapView"], None]] = []
        self.attribution = ""
        # координати камер (широта, довгота): центр при зумі і перевірка, чи камери на карті рівня
        self.camera_points: Callable[[], List[Tuple[float, float]]] = lambda: []

    # --- дані -------------------------------------------------------------------------------------------
    def set_levels(self, levels: List[mp.MapLevel]) -> None:
        current = self.current
        self.levels = sorted(levels, key=lambda lv: -lv.level)
        self._cache.clear()
        self._pyramid = {}
        self.attribution = next((lv.attribution for lv in self.levels if lv.attribution), "")
        if self.levels:
            if current is not None and any(lv.level == current.level for lv in self.levels):
                self.step = next(i for i, lv in enumerate(self.levels) if lv.level == current.level)
            else:
                self.step = self._best_step()
        self.recenter()
        self.level_changed.emit(self.index)
        self.update()

    def _cams_merc(self) -> List[Tuple[float, float]]:
        return [mp.lonlat_to_merc(lo, la) for la, lo in self.camera_points() if la is not None and lo is not None]

    def _best_step(self) -> int:
        """Найдетальніший рівень, на карті якого є камери; інакше — найменш детальний."""
        for i in range(len(self.levels) - 1, -1, -1):
            if self._has_cameras(self.levels[i]):
                return i
        return 0

    def _has_cameras(self, lv: mp.MapLevel) -> bool:
        cams = self._cams_merc()
        if not cams:
            return True
        a, b, c, d = lv.bounds
        return any(a <= x <= c and b <= y <= d for x, y in cams)

    @property
    def level_visible(self) -> bool:
        """Карта поточного рівня показується, лише якщо на ній є хоч одна камера."""
        cur = self.current
        return cur is not None and self._has_cameras(cur)

    def center_on(self, lat: float, lon: float) -> None:
        self.cx, self.cy = mp.lonlat_to_merc(lon, lat)
        self.update()

    def recenter(self) -> None:
        """Центр — середина між крайніми координатами камер; на рівні з картою зсувається так, щоб вигляд
        лишався на карті (менша за вигляд карта — посередині)."""
        cams = self._cams_merc()
        cur = self.current
        if cams:
            xs, ys = [c[0] for c in cams], [c[1] for c in cams]
            tx, ty = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        elif cur is not None:
            a, b, c, d = cur.bounds
            tx, ty = (a + c) / 2, (b + d) / 2
        else:
            return
        if cur is not None and self.level_visible and self.width() > 0:
            a, b, c, d = cur.bounds
            hw, hh = self.width() / 2 * self.res, self.height() / 2 * self.res
            tx = (a + c) / 2 if c - a <= 2 * hw else min(max(tx, a + hw), c - hw)
            ty = (b + d) / 2 if d - b <= 2 * hh else min(max(ty, b + hh), d - hh)
        self.cx, self.cy = tx, ty
        self.update()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self.recenter()

    @property
    def current(self) -> Optional[mp.MapLevel]:
        return self.levels[self.step] if self.levels and 0 <= self.step < len(self.levels) else None

    @property
    def whole(self) -> bool:
        """На кожному рівні карта вписується у вигляд цілком."""
        return bool(self.levels)

    @property
    def steps(self) -> int:
        return len(self.levels) if self.levels else len(EMPTY_STEPS)

    @property
    def index(self) -> int:
        return self.step if self.levels else self._empty_step

    def dpr(self) -> float:
        """Фізичних пікселів екрана на логічний (масштаб Windows 125 %, 150 % …)."""
        try:
            return max(1.0, float(self.devicePixelRatioF()))
        except RuntimeError:
            return 1.0

    @property
    def res(self) -> float:
        """Метрів EPSG:3857 на логічний піксель вигляду. Піксель файлу = фізичний піксель екрана: карта
        ніколи не збільшується (і не розмивається масштабом екрана); рівень 8 може лише зменшуватись."""
        cur = self.current
        if cur is None:
            return EMPTY_STEPS[self._empty_step]
        native = cur.res * self.dpr()
        if self.width() > 0 and self.height() > 0:
            a, b, c, d = cur.bounds                 # уся карта рівня — у вигляді (з полями 3 %)
            return max(native, (c - a) / self.width() * 1.03, (d - b) / self.height() * 1.03)
        return native

    def set_index(self, i: int) -> None:
        i = max(0, min(self.steps - 1, i))
        if i != self.index:
            if self.levels:
                self.step = i
            else:
                self._empty_step = i
            self.recenter()
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
        kind = ("найбільша деталізація" if self.index == self.steps - 1 else
                "найменша деталізація" if self.index == 0 else "")
        tail = "" if self.level_visible else " · камери поза межами карти"
        shrink = self.res / (cur.res * self.dpr())
        scale = " · уся карта" + (f", зменшено в {shrink:.1f} раза" if shrink > 1.05 else ", 1:1")
        return (f"Рівень {cur.level}{f' ({kind})' if kind else ''} · {os.path.basename(cur.source_file)} · "
                f"{cur.ground_res():.2f} м/піксель{scale}{tail}")

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
            self.set_index(self.index + (1 if steps > 0 else -1))      # центр — середина між камерами

    # --- тайли ---------------------------------------------------------------------------------------------------
    def _tile(self, lv: mp.MapLevel, tx: int, ty: int, pl: int = 0) -> Optional[QImage]:
        key = f"{lv.level}/{lv.loaded_at}/{pl}/{tx}/{ty}"
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        path = lv.tile_path(tx, ty, pl)
        img = QImage(path) if os.path.isfile(path) else None
        if img is not None and img.isNull():
            img = None
        self._cache[key] = img
        while len(self._cache) > 800:
            self._cache.popitem(last=False)
        return img

    def _draw_level(self, p: QPainter, lv: mp.MapLevel) -> int:
        """Тайли рівня, що потрапляють у вигляд. Повертає кількість."""
        r = self.res
        dpr = self.dpr()
        # зменшена копія: найбільша, що ще не дрібніша за фізичний піксель екрана
        pl = 0
        if lv.level not in self._pyramid:
            self._pyramid[lv.level] = lv.pyramid()
        while pl < self._pyramid[lv.level] and lv.res * 2 ** (pl + 1) <= r * dpr * 1.0001:
            pl += 1
        pres = lv.res * 2 ** pl                          # крок пікселя копії
        pw, ph = math.ceil(lv.width / 2 ** pl), math.ceil(lv.height / 2 ** pl)
        k = pres / r                                     # логічних пікселів на піксель копії (≤ 1 / dpr)
        size = TILE * k
        exact = abs(k * dpr - 1.0) < 1e-6               # 1 піксель копії = 1 фізичний піксель
        left = self.cx - self.width() / 2 * r            # межі вигляду в EPSG:3857
        top = self.cy + self.height() / 2 * r
        tx0 = max(0, int(math.floor((left - lv.x0) / (pres * TILE))))
        ty0 = max(0, int(math.floor((lv.y0 - top) / (pres * TILE))))
        tx1 = min(math.ceil(pw / TILE) - 1, int(math.floor((left + self.width() * r - lv.x0) / (pres * TILE))))
        ty1 = min(math.ceil(ph / TILE) - 1, int(math.floor((lv.y0 - (top - self.height() * r)) / (pres * TILE))))
        if tx1 < tx0 or ty1 < ty0 or (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > 2500:
            return 0
        n = 0
        ox = (lv.x0 - left) / r
        oy = (top - lv.y0) / r
        if exact:                                       # без згладжування і з вирівнюванням по пікселях
            ox, oy = round(ox * dpr) / dpr, round(oy * dpr) / dpr
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                img = self._tile(lv, tx, ty, pl)
                if img is not None:
                    p.drawImage(QRectF(ox + tx * size, oy + ty * size, size, size), img)
                    n += 1
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        return n

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.fillRect(self.rect(), QColor("#d9d9d4"))
        cur = self.current
        if cur is None:
            self._draw_grid(p, "Карту ще не завантажено («Налаштування / Карта»)")
        elif not self.level_visible:
            self._draw_grid(p, f"Камери поза межами карти рівня {cur.level} ({os.path.basename(cur.source_file)})")
        elif not self._draw_level(p, cur):
            self._draw_grid(p, "Тут карти рівня немає")
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for draw in self.overlays:
            draw(p, self)
        f = QFont(p.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        p.setFont(f)
        if self.attribution and self.level_visible:
            w = p.fontMetrics().horizontalAdvance(self.attribution) + 10
            box = QRectF(self.width() - w - 2, self.height() - 18, w, 16)
            p.fillRect(box, QColor(255, 255, 255, 200))
            p.setPen(QColor("#333"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, self.attribution)
        p.end()

    def _draw_grid(self, p: QPainter, text: str) -> None:
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
        p.drawText(QPointF(10, 20), text)


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


class CameraTriangles:
    """Шар карти: камера — трикутник поля зору. Вершина — у GPS-точці камери, кут = HFOV, напрям —
    поточний азимут від півночі (кут датчика + зміщення нуля), висота ≈ 70 px на будь-якому рівні.
    Без звʼязку з сервоприводами — останній відомий азимут (або зміщення нуля)."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.last_az = {}

    def azimuth(self, rec) -> Tuple[float, bool]:
        try:
            st = self.ctx.control.status(rec.name)
        except Exception:  # noqa: BLE001 — камера ще не підключена до керування
            st = None
        online = bool(st is not None and st.link_ok and st.azimuth_deg is not None)
        if online:
            self.last_az[rec.name] = st.azimuth_deg
        return self.last_az.get(rec.name, rec.north_offset_deg), online

    def __call__(self, p: QPainter, view: "MapView") -> None:
        for rec in self.ctx.records():
            if rec.latitude is None or rec.longitude is None:
                continue
            apex = view.to_screen(rec.latitude, rec.longitude)
            az, online = self.azimuth(rec)
            poly = triangle(apex, az, rec.fov_x_deg)
            p.setPen(QPen(QColor("#1e66d0") if online else QColor("#6b6b6b"), 2))
            p.setBrush(QColor(30, 102, 208, 90) if online else QColor(120, 120, 120, 80))
            p.drawPolygon(poly)
            p.setBrush(QColor("white"))
            p.drawEllipse(apex, 3.5, 3.5)
            p.setPen(QColor("#102a55"))
            p.drawText(apex + QPointF(8, 14), f"{rec.name} · {az:.0f}°")
