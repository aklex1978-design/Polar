"""Знімки камери під час Навчання на кожному кроці плану.

Крок знімків — половина кута огляду камери: по азимуту HFOV/2, по куту місця VFOV/2 (той самий крок,
що й у Навчання). Окремого проходу немає: на кожному кроці камера зупиняється, і кадр, записаний у
панораму, зберігається ще й окремим файлом. Кроки по азимуту вирівняні так, що азимут від півночі
кратний кроку (0°, HFOV/2, HFOV …).

Файли: <каталог>/el_<кут місця>/az_<азимут від півночі>.jpg і index.json з фактичними кутами.
Кнопка «Зберегти» на «Робота / Камери» дописує знімок: на вузлі — замінює знімок вузла, поза вузлом —
<каталог>/manual/el_<кут місця>_az_<азимут>.jpg.
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

    def load(self) -> None:
        """Продовжити наявний набір (ручне збереження): прочитати index.json, якщо він є."""
        os.makedirs(self.directory, exist_ok=True)
        try:
            with open(os.path.join(self.directory, "index.json"), encoding="utf-8") as fh:
                self.index = dict(json.load(fh).get("snapshots", {}))
        except (OSError, ValueError):
            self.index = {}

    def offer(self, sf) -> Optional[str]:
        """Зберегти кадр нерухомої камери, якщо він на вузлі. Повертає шлях або None."""
        if sf is None or not sf.has_angles or sf.angles.moving or sf.index == self._last_frame:
            return None
        self._last_frame = sf.index
        a = sf.angles
        az_north = wrap360(a.azimuth_deg + self.north_offset_deg)
        node = self.node(az_north, a.elevation_deg)
        if node is None:
            return None
        return self._write(sf, node, az_north, overwrite=False, manual=False)

    def save(self, sf) -> str:
        """Ручне збереження (кнопка «Зберегти»): на вузлі знімок вузла замінюється, поза вузлом —
        окремий файл у manual/ за фактичними кутами. Повертає шлях."""
        if sf is None or not sf.has_angles:
            raise ValueError("немає кадру з кутами")
        a = sf.angles
        az_north = wrap360(a.azimuth_deg + self.north_offset_deg)
        node = self.node(az_north, a.elevation_deg)
        manual = node is None
        if manual:
            node = (round(a.elevation_deg, 1), round(az_north, 1))
        path = self._write(sf, node, az_north, overwrite=True, manual=manual)
        if path is None:
            raise OSError(f"не вдалося записати знімок у {self.directory}")
        return path

    def _write(self, sf, node, az_north, overwrite: bool, manual: bool) -> Optional[str]:
        import cv2

        a = sf.angles
        el_s, az_s = _deg(node[0], 2), _deg(node[1], 3)
        key = f"manual/{el_s}/{az_s}" if manual else f"{el_s}/{az_s}"
        with self._lock:
            if key in self.index and not overwrite:
                return None
            if manual:
                path = os.path.join(self.directory, "manual", f"el_{el_s}_az_{az_s}.jpg")
            else:
                path = os.path.join(self.directory, f"el_{el_s}", f"az_{az_s}.jpg")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not cv2.imwrite(path, sf.image, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                return None
            entry = {
                "file": os.path.relpath(path, self.directory).replace(os.sep, "/"),
                "elevation_node": node[0], "azimuth_node": node[1],
                "azimuth_deg": round(az_north, 3), "sensor_deg": round(wrap360(a.azimuth_deg), 3),
                "elevation_deg": round(a.elevation_deg, 3),
                "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            }
            if manual or overwrite:
                entry["manual"] = True
            self.index[key] = entry
            with open(os.path.join(self.directory, "index.json"), "w", encoding="utf-8") as fh:
                json.dump({"azimuth_step_deg": self.az_step, "elevations_deg": list(self.elevations),
                           "snapshots": self.index}, fh, ensure_ascii=False, indent=1)
            return path

    @property
    def count(self) -> int:
        return sum(1 for k in self.index if not k.startswith("manual/"))

    @property
    def total(self) -> int:
        return len(self.elevations) * int(round(360.0 / self.az_step))


_INDEX_CACHE: Dict[str, Tuple[float, dict]] = {}


def read_index(directory: str) -> dict:
    """index.json набору знімків (з кешем за часом зміни файлу); порожній, якщо файлу немає."""
    path = os.path.join(directory, "index.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _INDEX_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    _INDEX_CACHE[path] = (mtime, data)
    return data


def nearest_snapshot(directory: str, az_north: float, el: float) -> Optional[Tuple[str, dict]]:
    """Знімок для положення камери: найближчий вузол, у клітинці якого (± пів кроку по азимуту і по куту
    місця) перебуває камера. Повертає (повний шлях, запис index.json) або None."""
    data = read_index(directory)
    snaps = data.get("snapshots") or {}
    if not snaps:
        return None
    az_step = float(data.get("azimuth_step_deg") or 360.0)
    rows = sorted(float(e) for e in data.get("elevations_deg") or [])
    el_step = min((b - a for a, b in zip(rows, rows[1:])), default=90.0)
    best, best_d = None, None
    for entry in snaps.values():
        d_az = abs(wrap180(az_north - float(entry.get("azimuth_node", entry.get("azimuth_deg", 0.0)))))
        d_el = abs(el - float(entry.get("elevation_node", entry.get("elevation_deg", 0.0))))
        if d_az > az_step / 2 + 1e-6 or d_el > el_step / 2 + 1e-6:
            continue
        d = (d_az / az_step) ** 2 + (d_el / el_step) ** 2
        if best_d is None or d < best_d:
            best, best_d = entry, d
    if best is None:
        return None
    path = os.path.join(directory, best["file"])
    return (path, best) if os.path.isfile(path) else None
