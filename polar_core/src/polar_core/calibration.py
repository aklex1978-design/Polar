"""Калібрування камери: центр кадру, перевірка похибки розміщення за орієнтиром, модель камери."""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .angles import angular_distance, direction_vector, vector_to_direction
from .projection import CameraModel

DEFAULT_ACCURACY_DEG = 0.5          # критерій точності за замовчуванням («Налаштування / Виявлення»)


@dataclass
class Calibration:
    camera_id: int
    center_x: Optional[float] = None       # у пікселях кадру розміром frame_w × frame_h
    center_y: Optional[float] = None
    frame_w: Optional[int] = None
    frame_h: Optional[int] = None
    placement_error_deg: Optional[float] = None
    updated_at: str = ""

    def center_for(self, size: Tuple[int, int]) -> Optional[Tuple[float, float]]:
        """Центр у пікселях кадру іншого розміру (якщо змінилась роздільна здатність потоку)."""
        if self.center_x is None or self.center_y is None:
            return None
        w0, h0 = self.frame_w or size[0], self.frame_h or size[1]
        return self.center_x * size[0] / w0, self.center_y * size[1] / h0


def load(conn: sqlite3.Connection, camera_id: int) -> Calibration:
    row = conn.execute("SELECT center_x, center_y, frame_w, frame_h, placement_error_deg, updated_at "
                       "FROM calibration WHERE camera_id = ?", (camera_id,)).fetchone()
    return Calibration(camera_id, *row) if row else Calibration(camera_id)


def save_center(conn: sqlite3.Connection, camera_id: int, cx: float, cy: float, frame_size: Tuple[int, int]) -> None:
    with conn:
        conn.execute(
            "INSERT INTO calibration (camera_id, center_x, center_y, frame_w, frame_h, updated_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now')) ON CONFLICT(camera_id) DO UPDATE SET "
            "center_x = excluded.center_x, center_y = excluded.center_y, frame_w = excluded.frame_w, "
            "frame_h = excluded.frame_h, updated_at = excluded.updated_at",
            (camera_id, float(cx), float(cy), int(frame_size[0]), int(frame_size[1])))


def save_placement_error(conn: sqlite3.Connection, camera_id: int, error_deg: float,
                         frame_size: Tuple[int, int]) -> None:
    with conn:
        cur = conn.execute("UPDATE calibration SET placement_error_deg = ?, updated_at = datetime('now') "
                           "WHERE camera_id = ?", (float(error_deg), camera_id))
        if cur.rowcount == 0:      # центр ще не зберігали — середина кадру
            conn.execute("INSERT INTO calibration (camera_id, center_x, center_y, frame_w, frame_h, "
                         "placement_error_deg, updated_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                         (camera_id, frame_size[0] / 2, frame_size[1] / 2, frame_size[0], frame_size[1],
                          float(error_deg)))


def accuracy_criterion(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key = 'detection.accuracy_deg'").fetchone()
    try:
        return float(row[0]) if row else DEFAULT_ACCURACY_DEG
    except ValueError:
        return DEFAULT_ACCURACY_DEG


def model_for(conn: sqlite3.Connection, rec, frame_size: Optional[Tuple[int, int]] = None) -> CameraModel:
    """Модель камери з параметрів камери і її калібрування (для кадру заданого розміру)."""
    size = frame_size or rec.frame_size
    calib = load(conn, rec.id) if rec.id else Calibration(0)
    m = CameraModel.from_record(rec, frame_size=size)
    c = calib.center_for(size)
    if c is not None:
        m.cx, m.cy = c
    return m


@dataclass
class LandmarkCheck:
    """Перевірка похибки розміщення: той самий орієнтир позначають на кадрах, знятих при різних
    положеннях камери. Якщо модель (центр, дисторсія, нахил) правильна, усі позначки дають той самий
    азимут і кут місця. Похибка — найбільше відхилення позначки від їх середнього напрямку."""
    observations: List[Tuple[float, float]] = field(default_factory=list)   # (азимут від півночі, кут місця)

    def add(self, az: float, el: float) -> None:
        self.observations.append((az % 360.0, el))

    def clear(self) -> None:
        self.observations.clear()

    def mean(self) -> Optional[Tuple[float, float]]:
        if not self.observations:
            return None
        vs = [direction_vector(a, e) for a, e in self.observations]
        x, y, z = (sum(c) / len(vs) for c in zip(*vs))
        if math.hypot(x, y, z) < 1e-12:
            return None
        return vector_to_direction(x, y, z)

    def error(self) -> Optional[float]:
        if len(self.observations) < 2:
            return None
        m = self.mean()
        return max(angular_distance(a, e, m[0], m[1]) for a, e in self.observations)
