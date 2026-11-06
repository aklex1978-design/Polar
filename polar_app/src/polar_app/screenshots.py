"""Знімки камери під час Навчання на кутах, кратних 10°: кут місця 10…80°, азимут від півночі 0…350°.

Окремого проходу немає: знімок зберігається паралельно з побудовою панорами, щойно кути кадру
опиняються в межах допуску від вузла сітки (і коли камера стоїть на кроці плану, і коли проходить
через вузол у русі). Знімок з руху замінюється знімком нерухомої камери, якщо такий трапиться.

Файли: <каталог>/el_<кут місця>/az_<азимут>.jpg і index.json з фактичними кутами кожного знімка.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import threading
from typing import Dict, Optional, Tuple

from polar_core.angles import wrap180, wrap360

ELEVATIONS = tuple(range(10, 81, 10))
AZ_STEP = 10


class GridSnapshots:
    def __init__(self, directory: str, north_offset_deg: float, tolerance_deg: float = 0.5,
                 elevations=ELEVATIONS, az_step: int = AZ_STEP) -> None:
        self.directory = directory
        self.north_offset_deg = north_offset_deg
        self.tolerance_deg = tolerance_deg
        self.elevations = tuple(elevations)
        self.az_step = az_step
        self.index: Dict[str, dict] = {}
        self._last_frame = -1
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Нове Навчання — нові знімки."""
        if os.path.isdir(self.directory):
            shutil.rmtree(self.directory)
        os.makedirs(self.directory, exist_ok=True)
        self.index = {}

    def node(self, az_north: float, el: float) -> Optional[Tuple[int, int]]:
        """Вузол сітки (кут місця, азимут), якщо кути в межах допуску від нього."""
        el_n = int(round(el / 10.0)) * 10
        if el_n not in self.elevations or abs(el - el_n) > self.tolerance_deg:
            return None
        az_n = int(round(az_north / self.az_step)) * self.az_step % 360
        if abs(wrap180(az_north - az_n)) > self.tolerance_deg:
            return None
        return el_n, az_n

    def offer(self, sf) -> Optional[str]:
        """Перевірити кадр з кутами; якщо він на вузлі сітки — зберегти. Повертає шлях або None."""
        import cv2

        if sf is None or not sf.has_angles or sf.index == self._last_frame:
            return None
        self._last_frame = sf.index
        a = sf.angles
        az_north = wrap360(a.azimuth_deg + self.north_offset_deg)
        node = self.node(az_north, a.elevation_deg)
        if node is None:
            return None
        key = f"{node[0]}/{node[1]}"
        with self._lock:
            prev = self.index.get(key)
            if prev is not None and (not prev["moving"] or a.moving):
                return None      # уже є знімок; замінюється лише знімок з руху — знімком нерухомої камери
            path = os.path.join(self.directory, f"el_{node[0]:02d}", f"az_{node[1]:03d}.jpg")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not cv2.imwrite(path, sf.image, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                return None
            self.index[key] = {
                "file": os.path.relpath(path, self.directory), "elevation_node": node[0], "azimuth_node": node[1],
                "azimuth_deg": round(az_north, 3), "sensor_deg": round(wrap360(a.azimuth_deg), 3),
                "elevation_deg": round(a.elevation_deg, 3), "moving": bool(a.moving),
                "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            }
            with open(os.path.join(self.directory, "index.json"), "w", encoding="utf-8") as fh:
                json.dump(self.index, fh, ensure_ascii=False, indent=1)
            return path

    @property
    def count(self) -> int:
        return len(self.index)

    @property
    def total(self) -> int:
        return len(self.elevations) * (360 // self.az_step)
