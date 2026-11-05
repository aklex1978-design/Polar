"""Кути: енкодер ↔ градуси, кут датчика ↔ азимут, напрямок ↔ піксель панорами 360°×90°.

Терміни ТЗ:
* кут датчика — кут осі камери за енкодером сервопривода (0..360 по горизонталі);
* азимут — кут від півночі за годинниковою стрілкою: азимут = кут датчика + зміщення від півночі;
* кут місця — кут над горизонтом, 0..90°.
"""
from __future__ import annotations

import math
from typing import Tuple

STEPS_PER_TURN = 4096
DEG_PER_STEP = 360.0 / STEPS_PER_TURN
PANORAMA_AZ_SPAN = 360.0
PANORAMA_EL_SPAN = 90.0


def wrap360(angle_deg: float) -> float:
    a = angle_deg % 360.0
    return 0.0 if a == 360.0 else a


def wrap180(angle_deg: float) -> float:
    return (angle_deg + 180.0) % 360.0 - 180.0


def steps_to_deg(steps: int) -> float:
    return steps * DEG_PER_STEP


def deg_to_steps(deg: float) -> int:
    return int(round(deg / DEG_PER_STEP))


def sensor_to_azimuth(sensor_deg: float, north_offset_deg: float) -> float:
    return wrap360(sensor_deg + north_offset_deg)


def azimuth_to_sensor(azimuth_deg: float, north_offset_deg: float) -> float:
    return wrap360(azimuth_deg - north_offset_deg)


def angular_distance(az1: float, el1: float, az2: float, el2: float) -> float:
    """Кут між двома напрямками (градуси), формула гаверсинуса."""
    p1, p2 = math.radians(el1), math.radians(el2)
    dl = math.radians(az2 - az1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h))))


def direction_vector(azimuth_deg: float, elevation_deg: float) -> Tuple[float, float, float]:
    """Одиничний вектор у системі «схід, північ, вгору» (ENU)."""
    a, e = math.radians(azimuth_deg), math.radians(elevation_deg)
    return (math.cos(e) * math.sin(a), math.cos(e) * math.cos(a), math.sin(e))


def vector_to_direction(x: float, y: float, z: float) -> Tuple[float, float]:
    az = wrap360(math.degrees(math.atan2(x, y)))
    el = math.degrees(math.atan2(z, math.hypot(x, y)))
    return az, el


class PanoramaGrid:
    """Прямокутна панорама: x — азимут 0..360 зліва направо, y — кут місця 90 (верх) .. 0 (низ)."""

    def __init__(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("розмір панорами має бути додатним")
        self.width = width
        self.height = height
        self.px_per_deg_x = width / PANORAMA_AZ_SPAN
        self.px_per_deg_y = height / PANORAMA_EL_SPAN

    @classmethod
    def from_resolution(cls, deg_per_px: float) -> "PanoramaGrid":
        return cls(int(round(PANORAMA_AZ_SPAN / deg_per_px)), int(round(PANORAMA_EL_SPAN / deg_per_px)))

    def to_pixel(self, azimuth_deg, elevation_deg):
        """Працює і з числами, і з масивами numpy."""
        x = (azimuth_deg % PANORAMA_AZ_SPAN) * self.px_per_deg_x
        y = (PANORAMA_EL_SPAN - elevation_deg) * self.px_per_deg_y
        return x, y

    def to_angles(self, x: float, y: float) -> Tuple[float, float]:
        return wrap360(x / self.px_per_deg_x), PANORAMA_EL_SPAN - y / self.px_per_deg_y
