"""Панорама камери 360° × 90°: запис кадрів за проєкцією зі згладженим зшиванням, покриття, збереження.

Розмір: k пікселів на градус = ширина кадру / HFOV (ТЗ), але не більше max_px_per_deg — щоб панорама
вміщалась у памʼять (1920 px при HFOV 60° дає 32 px/°, тобто 11 520 × 2 880; за замовчуванням обмеження
16 px/° → 5 760 × 1 440). x — азимут від півночі 0…360° зліва направо, y — кут місця 90° (верх) … 0° (низ).

Зшивання: кожен піксель панорами — зважене середнє кадрів, що його покрили; вага кадру спадає до його
країв (feathering), тож у зонах перекриття немає видимих швів.
"""
from __future__ import annotations

import json
import math
import os
from typing import Optional, Tuple

import numpy as np

from .projection import CameraModel

DEFAULT_MAX_PX_PER_DEG = 16.0


class Panorama:
    def __init__(self, px_per_deg: float, channels: int = 3) -> None:
        self.k = float(px_per_deg)
        self.width = int(round(360.0 * self.k))
        self.height = int(round(90.0 * self.k))
        self.channels = channels
        self.image = np.zeros((self.height, self.width, channels), np.uint8)
        self.weight = np.zeros((self.height, self.width), np.float32)

    @classmethod
    def for_camera(cls, frame_width: int, hfov_deg: float,
                   max_px_per_deg: float = DEFAULT_MAX_PX_PER_DEG) -> "Panorama":
        return cls(min(frame_width / hfov_deg, max_px_per_deg))

    # --- координати ----------------------------------------------------------------------
    def x_of(self, az):
        return np.asarray(az) % 360.0 * self.k

    def y_of(self, el):
        return (90.0 - np.asarray(el)) * self.k

    def az_of(self, x):
        return (np.asarray(x) + 0.5) / self.k

    def el_of(self, y):
        return 90.0 - (np.asarray(y) + 0.5) / self.k

    # --- запис кадру -------------------------------------------------------------------------
    def _region(self, model: CameraModel, sensor_az: float, el: float) -> Tuple[int, int, int, int]:
        """Ділянка панорами (x0, x1, y0, y1), яку може покрити кадр; x1 може бути > width (через 360°)."""
        out = model.frame_outline(sensor_az, el, per_edge=40)
        top = out[:, 1].max()
        _u, _v, zen_ok = model.angles_to_pixel(0.0, 90.0, sensor_az, el)
        zenith_in = bool(zen_ok) and 0 <= _u < model.width and 0 <= _v < model.height
        el_hi = 90.0 if zenith_in else min(90.0, top + 0.5)
        el_lo = max(0.0, out[:, 1].min() - 0.5)
        y0 = int(math.floor(self.y_of(el_hi)))
        y1 = int(math.ceil(self.y_of(el_lo)))
        if zenith_in or el_hi >= 89.5:
            return 0, self.width, max(0, y0), min(self.height, y1)
        az = np.degrees(np.unwrap(np.radians(out[:, 0])))
        a0, a1 = az.min() - 0.5, az.max() + 0.5
        if a1 - a0 >= 359.0:
            return 0, self.width, max(0, y0), min(self.height, y1)
        x0 = int(math.floor((a0 % 360.0) * self.k))
        x1 = x0 + int(math.ceil((a1 - a0) * self.k))
        return x0, x1, max(0, y0), min(self.height, y1)

    def add_frame(self, image: np.ndarray, model: CameraModel, sensor_az: float, el: float) -> int:
        """Записати кадр, знятий при куті датчика sensor_az і куті місця el. Повертає кількість пікселів."""
        import cv2

        if image.ndim == 2:
            image = np.repeat(image[:, :, None], 3, axis=2)
        h, w = image.shape[:2]
        if (w, h) != (model.width, model.height):
            raise ValueError(f"кадр {w}×{h}, а модель камери — {model.width}×{model.height}")
        x0, x1, y0, y1 = self._region(model, sensor_az, el)
        if y1 <= y0 or x1 <= x0:
            return 0
        xs = np.arange(x0, x1)
        ys = np.arange(y0, y1)
        az = self.az_of(xs)[None, :].repeat(len(ys), 0)
        elv = self.el_of(ys)[:, None].repeat(len(xs), 1)
        u, v, ok = model.angles_to_pixel(az, elv, sensor_az, el)
        inside = ok & (u >= 0) & (u <= w - 1) & (v >= 0) & (v <= h - 1)
        if not inside.any():
            return 0
        mu = np.where(inside, u, -1).astype(np.float32)
        mv = np.where(inside, v, -1).astype(np.float32)
        patch = cv2.remap(image, mu, mv, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        # вага спадає до країв кадру: 1 у центрі, 0 на краю
        fw = np.minimum(np.minimum(u, w - 1 - u) / (w / 2), np.minimum(v, h - 1 - v) / (h / 2))
        fw = np.where(inside, np.clip(fw, 0.0, 1.0) + 1e-3, 0.0).astype(np.float32)
        cols = xs % self.width
        sub_w = self.weight[y0:y1][:, cols]
        sub_i = self.image[y0:y1][:, cols].astype(np.float32)
        tot = sub_w + fw
        blend = np.where(tot[..., None] > 0,
                         (sub_i * sub_w[..., None] + patch.astype(np.float32) * fw[..., None])
                         / np.maximum(tot, 1e-9)[..., None], sub_i)
        self.image[y0:y1, cols] = np.clip(blend + 0.5, 0, 255).astype(np.uint8)
        self.weight[y0:y1, cols] = tot
        return int(inside.sum())

    # --- покриття ----------------------------------------------------------------------------
    def covered(self) -> np.ndarray:
        return self.weight > 0

    def coverage(self) -> float:
        """Частка панорами 360° × 90°, на яку вже записано кадри (0…1)."""
        return float(self.covered().mean())

    def gaps(self, min_area_px: int = 1) -> int:
        """Кількість непокритих ділянок (для перевірки «без пропусків»)."""
        import cv2

        n, _labels, stats, _ = cv2.connectedComponentsWithStats((~self.covered()).astype(np.uint8), 8)
        return int(sum(1 for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_area_px))

    # --- збереження ---------------------------------------------------------------------------
    def save(self, path: str) -> None:
        """PNG панорами + PNG покриття + опис (k пікселів на градус)."""
        import cv2

        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        base, _ext = os.path.splitext(path)
        if not cv2.imwrite(path, self.image):
            raise IOError(f"не вдалося записати {path}")
        cv2.imwrite(base + ".coverage.png", (self.covered() * 255).astype(np.uint8))
        with open(base + ".json", "w", encoding="utf-8") as fh:
            json.dump({"px_per_deg": self.k, "width": self.width, "height": self.height,
                       "coverage": self.coverage()}, fh)

    @classmethod
    def load(cls, path: str) -> "Panorama":
        import cv2

        base, _ext = os.path.splitext(path)
        with open(base + ".json", encoding="utf-8") as fh:
            meta = json.load(fh)
        p = cls(meta["px_per_deg"])
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None or img.shape[:2] != (p.height, p.width):
            raise IOError(f"файл панорами {path} пошкоджено або має інший розмір")
        p.image = img
        cov = cv2.imread(base + ".coverage.png", cv2.IMREAD_GRAYSCALE)
        p.weight = (cov > 0).astype(np.float32) if cov is not None else (img.max(axis=2) > 0).astype(np.float32)
        return p

    @staticmethod
    def exists(path: Optional[str]) -> bool:
        return bool(path) and os.path.isfile(path) and os.path.isfile(os.path.splitext(path)[0] + ".json")
