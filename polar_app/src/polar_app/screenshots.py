"""Знімки камери під час Навчання на кожному кроці плану.

Крок знімків — половина кута огляду камери: по азимуту HFOV/2, по куту місця VFOV/2 (той самий крок,
що й у Навчання). Окремого проходу немає: на кожному кроці камера зупиняється, і кадр, записаний у
панораму, зберігається ще й окремим файлом. Кроки по азимуту вирівняні так, що азимут від півночі
кратний кроку (0°, HFOV/2, HFOV …).

Файли: <каталог>/el_<кут місця>/az_<азимут від півночі>.jpg і index.json з фактичними кутами.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import threading
from typing import Dict, Iterable, Optional, Tuple

from polar_core.angles import wrap180, wrap360


def _deg(v: float, width: int) -> str:
    """Кут для назви файлу: 030, 022.5."""
    return f"{v:0{width}.0f}" if abs(v - round(v)) < 1e-6 else f"{v:0{width + 2}.1f}"


class GridSnapshots:
    def __init__(self, directory: str, north_offset_deg: float, az_step_deg: float,
                 elevations: Iterable[float], tolerance_deg: float = 0.5) -> None:
        self.directory = directory
        self.north_offset_deg = north_offset_deg
        self.az_step = float(az_step_deg)
        self.elevations = tuple(sorted(float(e) for e in elevations))
        self.tolerance_deg = tolerance_deg
        self.index: Dict[str, dict] = {}
        self._last_frame = -1
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Нове Навчання — нові знімки."""
        if os.path.isdir(self.directory):
            shutil.rmtree(self.directory)
        os.makedirs(self.directory, exist_ok=True)
        self.index = {}

    def node(self, az_north: float, el: float) -> Optional[Tuple[float, float]]:
        """Вузол (кут місця, азимут від півночі), якщо кути в межах допуску від нього."""
        el_n = min(self.elevations, key=lambda e: abs(e - el), default=None)
        if el_n is None or abs(el - el_n) > self.tolerance_deg:
            return None
        az_n = (round(az_north / self.az_step) * self.az_step) % 360.0
        if abs(wrap180(az_north - az_n)) > self.tolerance_deg:
            return None
        return el_n, round(az_n, 6)

    def offer(self, sf) -> Optional[str]:
        """Зберегти кадр нерухомої камери, якщо він на вузлі. Повертає шлях або None."""
        import cv2

        if sf is None or not sf.has_angles or sf.angles.moving or sf.index == self._last_frame:
            return None
        self._last_frame = sf.index
        a = sf.angles
        az_north = wrap360(a.azimuth_deg + self.north_offset_deg)
        node = self.node(az_north, a.elevation_deg)
        if node is None:
            return None
        key = f"{_deg(node[0], 2)}/{_deg(node[1], 3)}"
        with self._lock:
            if key in self.index:
                return None
            path = os.path.join(self.directory, f"el_{_deg(node[0], 2)}", f"az_{_deg(node[1], 3)}.jpg")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not cv2.imwrite(path, sf.image, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                return None
            self.index[key] = {
                "file": os.path.relpath(path, self.directory), "elevation_node": node[0], "azimuth_node": node[1],
                "azimuth_deg": round(az_north, 3), "sensor_deg": round(wrap360(a.azimuth_deg), 3),
                "elevation_deg": round(a.elevation_deg, 3),
                "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            }
            with open(os.path.join(self.directory, "index.json"), "w", encoding="utf-8") as fh:
                json.dump({"azimuth_step_deg": self.az_step, "elevations_deg": list(self.elevations),
                           "snapshots": self.index}, fh, ensure_ascii=False, indent=1)
            return path

    @property
    def count(self) -> int:
        return len(self.index)

    @property
    def total(self) -> int:
        return len(self.elevations) * int(round(360.0 / self.az_step))
