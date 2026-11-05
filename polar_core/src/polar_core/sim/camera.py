"""Імітатор камери: кадр із синтетичної сцени 360°×90° за кутами камери, з рухомими цілями.

Проєкція — ідеальна камера-обскура (без дисторсії, нахил 0). Служить для розробки й тестів
побудови панорами та виявлення руху без обладнання.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from ..angles import PanoramaGrid, wrap360


def make_scene(width: int = 3600, height: int = 900, seed: int = 0) -> np.ndarray:
    """Синтетична сцена (сірі тони uint8): небо з градієнтом, «хмари», лінія горизонту з «будівлями»."""
    rng = np.random.default_rng(seed)
    y = np.linspace(1.0, 0.0, height)[:, None]          # 1 — зеніт, 0 — горизонт
    scene = 150 + 80 * y * np.ones((1, width))
    for _ in range(40):                                  # хмари
        cx, cy = rng.uniform(0, width), rng.uniform(0, height * 0.7)
        r = rng.uniform(15, 60)
        xx = np.arange(width)[None, :]
        dx = np.minimum(np.abs(xx - cx), width - np.abs(xx - cx))
        d2 = dx ** 2 + (np.arange(height)[:, None] - cy) ** 2
        scene += 25 * np.exp(-d2 / (2 * r * r))
    skyline = (height - rng.integers(5, height // 8, size=width // 20).repeat(20)[:width])
    rows = np.arange(height)[:, None]
    scene = np.where(rows >= skyline[None, :], 60.0, scene)
    return np.clip(scene, 0, 255).astype(np.uint8)


@dataclass
class SimTarget:
    """Ціль, що рухається рівномірно по кутах."""
    azimuth_deg: float
    elevation_deg: float
    d_az_deg_s: float = 0.0
    d_el_deg_s: float = 0.0
    size_deg: float = 0.5
    brightness: int = 20

    def at(self, t: float) -> Tuple[float, float]:
        return wrap360(self.azimuth_deg + self.d_az_deg_s * t), self.elevation_deg + self.d_el_deg_s * t


class SimCamera:
    def __init__(self, scene: np.ndarray, resolution: Tuple[int, int] = (640, 480),
                 fov_deg: Tuple[float, float] = (60.0, 45.0), noise_std: float = 0.0,
                 seed: Optional[int] = None, model=None) -> None:
        """model — polar_core.projection.CameraModel: тоді кадр малюється з дисторсією, нахилом γ
        і зсунутим центром цієї моделі (кути — кути датчика, без зміщення від півночі)."""
        self.model = model
        self.scene = scene
        self.grid = PanoramaGrid(scene.shape[1], scene.shape[0])
        self.width, self.height = resolution
        self.fov_x, self.fov_y = fov_deg
        self.noise_std = noise_std
        self.targets: List[SimTarget] = []
        self._rng = np.random.default_rng(seed)
        self.fx = (self.width / 2) / math.tan(math.radians(self.fov_x / 2))
        self.fy = (self.height / 2) / math.tan(math.radians(self.fov_y / 2))

    def pixel_directions(self, azimuth_deg: float, elevation_deg: float) -> Tuple[np.ndarray, np.ndarray]:
        """Азимут і кут місця для кожного пікселя кадру (масиви height×width)."""
        if self.model is not None:
            u, v = np.meshgrid(np.arange(self.width) + 0.5, np.arange(self.height) + 0.5)
            return self.model.pixel_to_angles(u, v, azimuth_deg, elevation_deg, north=False)
        u = (np.arange(self.width) + 0.5 - self.width / 2) / self.fx
        v = (self.height / 2 - (np.arange(self.height) + 0.5)) / self.fy
        u, v = np.meshgrid(u, v)
        # промінь у системі камери: вперед (1), вправо (u), вгору (v) → поворот на кут місця, потім азимут
        e = math.radians(elevation_deg)
        fwd = np.cos(e) * 1 - np.sin(e) * v
        up = np.sin(e) * 1 + np.cos(e) * v
        az = np.degrees(np.arctan2(u, fwd)) + azimuth_deg
        el = np.degrees(np.arctan2(up, np.hypot(u, fwd)))
        return np.mod(az, 360.0), el

    def capture(self, azimuth_deg: float, elevation_deg: float, t: float = 0.0) -> np.ndarray:
        az, el = self.pixel_directions(azimuth_deg, elevation_deg)
        x, y = self.grid.to_pixel(az, np.clip(el, 0.0, 90.0))
        xi = np.clip(x.astype(int), 0, self.grid.width - 1)
        yi = np.clip(y.astype(int), 0, self.grid.height - 1)
        frame = self.scene[yi, xi].astype(np.float32)
        frame[el < 0] = 40.0                             # нижче горизонту — земля
        for tgt in self.targets:
            taz, tel = tgt.at(t)
            d_az = (az - taz + 180.0) % 360.0 - 180.0
            mask = (d_az * math.cos(math.radians(tel))) ** 2 + (el - tel) ** 2 <= (tgt.size_deg / 2) ** 2
            frame[mask] = tgt.brightness
        if self.noise_std:
            frame += self._rng.normal(0, self.noise_std, frame.shape)
        return np.clip(frame, 0, 255).astype(np.uint8)
