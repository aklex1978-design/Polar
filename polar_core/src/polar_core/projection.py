"""Модель камери і проєкція: піксель кадру ↔ азимут і кут місця.

Параметри (ТЗ, «Калібрування» і «Редагування»): ширина кадру W, кут огляду HFOV, центр камери (c_x, c_y),
дисторсія k1, k2, p1, p2, k3 (модель Брауна–Конраді, як в OpenCV), нахил камери γ відносно вертикалі
(за годинниковою стрілкою) і зміщення нуля датчика від півночі.

Ланцюжок «піксель → кути»:
1. (u, v) → нормовані координати з дисторсією: x_d = (u − c_x) / f, y_d = (v − c_y) / f, f = (W/2) / tan(HFOV/2);
2. прибрати дисторсію (ітераційно) → (x, y) у системі матриці камери;
3. повернути на γ (як у формулі ТЗ) → (x', y') у системі осей поворотного пристрою;
4. промінь (вперед 1, праворуч x', вниз y') повернути на кут місця і азимут камери → азимут і кут місця точки.

Крок 4 — точний поворот променя на сфері. Формула ТЗ θ = θ₀ + arctan(x'/f), φ = φ₀ − arctan(y'/f) — його
наближення: збігається біля горизонту, але на великих кутах місця помиляється на градуси, тож тут
використовується точна форма. «Кути камери» нижче — кут датчика (без зміщення від півночі) і кут місця.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union

import numpy as np

ArrayLike = Union[float, np.ndarray]


@dataclass
class CameraModel:
    width: int
    height: int
    hfov_deg: float
    vfov_deg: float
    cx: Optional[float] = None          # None — середина кадру
    cy: Optional[float] = None
    k1: float = 0.0
    k2: float = 0.0
    p1: float = 0.0
    p2: float = 0.0
    k3: float = 0.0
    tilt_deg: float = 0.0               # γ: нахил за годинниковою стрілкою відносно вертикалі
    north_offset_deg: float = 0.0       # азимут = кут датчика + зміщення від півночі

    def __post_init__(self) -> None:
        if self.cx is None:
            self.cx = self.width / 2.0
        if self.cy is None:
            self.cy = self.height / 2.0

    @classmethod
    def from_record(cls, rec, center: Optional[Tuple[float, float]] = None,
                    frame_size: Optional[Tuple[int, int]] = None) -> "CameraModel":
        """З запису камери (polar_core.cameras.CameraRecord) і центру з калібрування.
        frame_size — фактичний розмір кадру, якщо потік віддає інший; центр масштабується."""
        w0, h0 = rec.frame_size
        w, h = frame_size or (w0, h0)
        cx = cy = None
        if center is not None:
            cx, cy = center[0] * w / w0, center[1] * h / h0
        return cls(w, h, rec.fov_x_deg, rec.fov_y_deg, cx, cy, rec.k1, rec.k2, rec.p1, rec.p2, rec.k3,
                   rec.tilt_deg, rec.north_offset_deg)

    # --- оптика -----------------------------------------------------------------------
    @property
    def f(self) -> float:
        return (self.width / 2.0) / math.tan(math.radians(self.hfov_deg) / 2.0)

    @property
    def has_distortion(self) -> bool:
        return any((self.k1, self.k2, self.p1, self.p2, self.k3))

    def distort(self, x: ArrayLike, y: ArrayLike) -> Tuple[ArrayLike, ArrayLike]:
        r2 = x * x + y * y
        radial = 1 + self.k1 * r2 + self.k2 * r2 * r2 + self.k3 * r2 * r2 * r2
        xd = x * radial + 2 * self.p1 * x * y + self.p2 * (r2 + 2 * x * x)
        yd = y * radial + self.p1 * (r2 + 2 * y * y) + 2 * self.p2 * x * y
        return xd, yd

    def undistort(self, xd: ArrayLike, yd: ArrayLike, iterations: int = 30) -> Tuple[ArrayLike, ArrayLike]:
        if not self.has_distortion:
            return xd, yd
        x, y = xd, yd
        for _ in range(iterations):              # ітерації як у cv2.undistortPoints
            r2 = x * x + y * y
            radial = 1 + self.k1 * r2 + self.k2 * r2 * r2 + self.k3 * r2 * r2 * r2
            dx = 2 * self.p1 * x * y + self.p2 * (r2 + 2 * x * x)
            dy = self.p1 * (r2 + 2 * y * y) + 2 * self.p2 * x * y
            x = (xd - dx) / radial
            y = (yd - dy) / radial
        return x, y

    def _tilt(self, x, y, sign: float = 1.0):
        g = math.radians(self.tilt_deg) * sign
        c, s = math.cos(g), math.sin(g)
        return x * c - y * s, x * s + y * c

    # --- піксель → кути --------------------------------------------------------------------
    def pixel_to_ray(self, u: ArrayLike, v: ArrayLike) -> Tuple[ArrayLike, ArrayLike]:
        """Піксель → (x', y'): тангенси вправо і вниз у системі осей поворотного пристрою."""
        f = self.f
        x, y = self.undistort((np.asarray(u, float) - self.cx) / f, (np.asarray(v, float) - self.cy) / f)
        return self._tilt(x, y)

    def pixel_to_angles(self, u: ArrayLike, v: ArrayLike, sensor_az_deg: float, elevation_deg: float,
                        north: bool = True) -> Tuple[ArrayLike, ArrayLike]:
        """Піксель кадру → (азимут, кут місця). north=True — азимут від півночі, інакше кут датчика."""
        x, y = self.pixel_to_ray(u, v)
        az0 = sensor_az_deg + (self.north_offset_deg if north else 0.0)
        a, e = math.radians(az0), math.radians(elevation_deg)
        sa, ca, se, ce = math.sin(a), math.cos(a), math.sin(e), math.cos(e)
        # базис камери у світі (схід, північ, вгору): вперед F, праворуч R, вгору U
        east = ce * sa + x * ca + y * se * sa
        nrth = ce * ca - x * sa + y * se * ca
        up = se - y * ce
        az = np.degrees(np.arctan2(east, nrth)) % 360.0
        el = np.degrees(np.arctan2(up, np.hypot(east, nrth)))
        return _scalar(az), _scalar(el)

    # --- кути → піксель -----------------------------------------------------------------------
    def angles_to_pixel(self, az_deg: ArrayLike, el_deg: ArrayLike, sensor_az_deg: float, elevation_deg: float,
                        north: bool = True) -> Tuple[ArrayLike, ArrayLike, ArrayLike]:
        """(азимут, кут місця) → (u, v, видно): видно=False для напрямків позаду камери."""
        az0 = sensor_az_deg + (self.north_offset_deg if north else 0.0)
        a, e = math.radians(az0), math.radians(elevation_deg)
        sa, ca, se, ce = math.sin(a), math.cos(a), math.sin(e), math.cos(e)
        pa, pe = np.radians(np.asarray(az_deg, float)), np.radians(np.asarray(el_deg, float))
        wx, wy, wz = np.cos(pe) * np.sin(pa), np.cos(pe) * np.cos(pa), np.sin(pe)
        fwd = wx * ce * sa + wy * ce * ca + wz * se
        right = wx * ca - wy * sa
        down = wx * se * sa + wy * se * ca - wz * ce            # = −(w·U)
        ok = fwd > 1e-9
        safe = np.where(ok, fwd, 1.0)
        x, y = self._tilt(right / safe, down / safe, sign=-1.0)
        xd, yd = self.distort(x, y)
        u, v = self.cx + self.f * xd, self.cy + self.f * yd
        return _scalar(u), _scalar(v), _scalar(ok)

    # --- побудови для екрана калібрування ----------------------------------------------------
    def fov_circle(self, fraction: float, steps: int = 180) -> np.ndarray:
        """Коло частки p кута огляду навколо центру камери (ТЗ: θ = p × HFOV / 2) у пікселях, з дисторсією."""
        theta = math.radians(fraction * self.hfov_deg / 2.0)
        phi = np.linspace(0.0, 2 * math.pi, steps + 1)
        x, y = math.tan(theta) * np.cos(phi), math.tan(theta) * np.sin(phi)
        xd, yd = self.distort(x, y)
        return np.stack([self.cx + self.f * xd, self.cy + self.f * yd], axis=1)

    @staticmethod
    def circle_angle(fraction: float, hfov_deg: float) -> float:
        return fraction * hfov_deg / 2.0

    def frame_outline(self, sensor_az_deg: float, elevation_deg: float, per_edge: int = 24,
                      north: bool = True) -> np.ndarray:
        """Точки вздовж чотирьох країв кадру → (азимут, кут місця): фігура поля зору на панорамі."""
        w, h = self.width - 1, self.height - 1
        t = np.linspace(0.0, 1.0, per_edge, endpoint=False)
        us = np.concatenate([t * w, np.full_like(t, w), (1 - t) * w, np.zeros_like(t)])
        vs = np.concatenate([np.zeros_like(t), t * h, np.full_like(t, h), (1 - t) * h])
        az, el = self.pixel_to_angles(us, vs, sensor_az_deg, elevation_deg, north=north)
        return np.stack([az, el], axis=1)

    def center_angles(self, sensor_az_deg: float, elevation_deg: float) -> Tuple[float, float]:
        """Куди дивиться центр камери (з урахуванням нахилу γ це не завжди кути датчика)."""
        return self.pixel_to_angles(self.cx, self.cy, sensor_az_deg, elevation_deg)


def split_wrapped(points: np.ndarray, period: float = 360.0) -> List[np.ndarray]:
    """Розрізати ламану (азимут, кут місця) там, де вона перетинає 0°/360° — для малювання на панорамі.
    Повертає частини; кожна частина продовжена за край, щоб фігура доходила до межі панорами."""
    if len(points) == 0:
        return []
    az = np.unwrap(np.radians(points[:, 0]))
    az = np.degrees(az)
    pts = np.stack([az, points[:, 1]], axis=1)
    lo, hi = math.floor(az.min() / period), math.floor(az.max() / period)
    parts = []
    for k in range(lo, hi + 1):
        part = pts.copy()
        part[:, 0] -= k * period
        parts.append(part)                       # частина в [0, 360) — інші за краєм обрізає малювання
    return parts


def angular_error(a: Sequence[float], b: Sequence[float]) -> float:
    """Кут між двома напрямками (азимут, кут місця), градуси."""
    from .angles import angular_distance

    return angular_distance(a[0], a[1], b[0], b[1])


def _scalar(x):
    a = np.asarray(x)
    return a.item() if a.ndim == 0 else a
