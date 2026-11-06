"""Перетворення координат: WGS-84 (широта, довгота, висота) ↔ місцеві метри ENU (схід, північ, вгору).

Перетворення точні (через ECEF), без наближення «плаский світ», тож придатні й для кілометрових
відстаней. Висоти — над рівнем моря так, як їх задано для камер (геоїд не враховується: для
тріангуляції важливо лише, щоб усі висоти були в одній системі).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Tuple

import numpy as np

A = 6378137.0                   # велика піввісь WGS-84, м
F = 1 / 298.257223563
E2 = F * (2 - F)                # квадрат ексцентриситету
B = A * (1 - F)
EP2 = (A * A - B * B) / (B * B)


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float
    altitude_m: float = 0.0


def to_ecef(lat: float, lon: float, alt: float) -> np.ndarray:
    phi, lam = math.radians(lat), math.radians(lon)
    s, c = math.sin(phi), math.cos(phi)
    n = A / math.sqrt(1 - E2 * s * s)
    return np.array([(n + alt) * c * math.cos(lam), (n + alt) * c * math.sin(lam), (n * (1 - E2) + alt) * s])


def from_ecef(x: float, y: float, z: float) -> Tuple[float, float, float]:
    """ECEF → (широта, довгота, висота); формула Боурінга з одним уточненням (похибка ≪ 1 мм)."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    th = math.atan2(z * A, p * B)
    lat = math.atan2(z + EP2 * B * math.sin(th) ** 3, p - E2 * A * math.cos(th) ** 3)
    for _ in range(2):
        s = math.sin(lat)
        n = A / math.sqrt(1 - E2 * s * s)
        alt = p / math.cos(lat) - n if abs(math.cos(lat)) > 1e-12 else abs(z) - B
        lat = math.atan2(z, p * (1 - E2 * n / (n + alt)))
    s = math.sin(lat)
    n = A / math.sqrt(1 - E2 * s * s)
    alt = p / math.cos(lat) - n if abs(math.cos(lat)) > 1e-12 else abs(z) - B
    return math.degrees(lat), math.degrees(lon), alt


class LocalFrame:
    """Місцева система ENU з початком у точці origin (центр карти або середина камер)."""

    def __init__(self, origin: GeoPoint) -> None:
        self.origin = origin
        self._o = to_ecef(origin.latitude, origin.longitude, origin.altitude_m)
        phi, lam = math.radians(origin.latitude), math.radians(origin.longitude)
        sp, cp, sl, cl = math.sin(phi), math.cos(phi), math.sin(lam), math.cos(lam)
        # рядки — орти east, north, up у ECEF
        self._r = np.array([[-sl, cl, 0.0],
                            [-sp * cl, -sp * sl, cp],
                            [cp * cl, cp * sl, sp]])

    @classmethod
    def around(cls, points: Iterable[GeoPoint]) -> "LocalFrame":
        pts = list(points)
        if not pts:
            raise ValueError("немає точок для початку місцевої системи")
        return cls(GeoPoint(sum(p.latitude for p in pts) / len(pts), sum(p.longitude for p in pts) / len(pts),
                            sum(p.altitude_m for p in pts) / len(pts)))

    def to_enu(self, p: GeoPoint) -> np.ndarray:
        return self._r @ (to_ecef(p.latitude, p.longitude, p.altitude_m) - self._o)

    def direction(self, src: GeoPoint, azimuth_deg: float, elevation_deg: float) -> np.ndarray:
        """Одиничний напрям у цій системі для азимуту і кута місця, виміряних у точці src (за її
        місцевою північчю і вертикаллю; на кілометрах вони відрізняються від осей початку системи)."""
        th, ph = math.radians(azimuth_deg), math.radians(elevation_deg)
        d_src = np.array([math.cos(ph) * math.sin(th), math.cos(ph) * math.cos(th), math.sin(ph)])
        r_src = LocalFrame(src)._r
        d = self._r @ (r_src.T @ d_src)
        return d / np.linalg.norm(d)

    def to_geo(self, enu) -> GeoPoint:
        x = self._o + self._r.T @ np.asarray(enu, float)
        return GeoPoint(*from_ecef(*x))


def bearing_elevation(frame: LocalFrame, src: GeoPoint, dst: GeoPoint) -> Tuple[float, float, float]:
    """Азимут від півночі, кут місця і пряма відстань від src до dst — як їх бачить камера в src
    (за місцевою вертикаллю самої камери, а не початку системи)."""
    local = LocalFrame(src)
    v = local.to_enu(dst)
    d = float(np.linalg.norm(v))
    az = math.degrees(math.atan2(v[0], v[1])) % 360.0
    el = math.degrees(math.asin(v[2] / d)) if d > 0 else 0.0
    return az, el, d
