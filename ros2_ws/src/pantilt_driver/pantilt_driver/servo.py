"""Налаштування сервопривода і перерахунок кутів сервопривода в кути осі камери.

Кут сервопривода (raw) — кут вала ST3020 у градусах, з урахуванням багатооборотного режиму
(може виходити за 0..360). Кут осі (axis) — кут камери: для азимуту 0..360, для кута місця 0..90.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

STEPS_PER_TURN = 4096          # 12-бітний магнітний енкодер ST3020
DEG_PER_STEP = 360.0 / STEPS_PER_TURN
ST3020_MAX_SPEED_DEG_S = 360.0  # ≈ 60 об/хв без навантаження при 12 В
MULTITURN_LIMIT_DEG = 7.5 * 360.0  # багатооборотний режим: до 7,5 оберту в кожен бік


@dataclass
class ServoConfig:
    servo_id: int
    direction: int = 1              # +1 прямий, -1 зворотний
    zero_offset_deg: float = 0.0    # кут сервопривода, що відповідає 0° осі
    gear_ratio: float = 1.0         # редуктора немає — 1
    max_speed_deg_s: float = 180.0
    acceleration: int = 50          # одиниці сервопривода
    torque_limit_pct: float = 100.0
    temperature_limit_c: float = 70.0
    min_deg: Optional[float] = None  # None = без меж (вісь азимуту з ковзним контактом)
    max_deg: Optional[float] = None
    continuous: bool = False        # True для азимуту: обертання без обмежень через 0/360

    def __post_init__(self) -> None:
        if not 1 <= self.servo_id <= 253:
            raise ValueError(f"ID сервопривода має бути 1..253, отримано {self.servo_id}")
        if self.direction not in (1, -1):
            raise ValueError("direction має бути +1 або -1")
        if self.gear_ratio <= 0:
            raise ValueError("gear_ratio має бути > 0")
        if not 0 < self.max_speed_deg_s <= ST3020_MAX_SPEED_DEG_S * self.gear_ratio:
            raise ValueError("max_speed_deg_s поза межами можливостей ST3020")

    # --- перерахунок кутів -------------------------------------------------
    def raw_to_axis(self, raw_deg: float) -> float:
        """Кут вала сервопривода → кут осі камери (без зведення до 0..360)."""
        return self.direction * (raw_deg - self.zero_offset_deg) / self.gear_ratio

    def axis_to_raw(self, axis_deg: float) -> float:
        """Кут осі камери → кут вала сервопривода."""
        return self.zero_offset_deg + self.direction * axis_deg * self.gear_ratio

    def clamp_axis(self, axis_deg: float) -> float:
        if self.min_deg is not None:
            axis_deg = max(axis_deg, self.min_deg)
        if self.max_deg is not None:
            axis_deg = min(axis_deg, self.max_deg)
        return axis_deg

    def limit_speed(self, speed_deg_s: float) -> float:
        """0 або від'ємне = найбільша швидкість з налаштувань."""
        if speed_deg_s <= 0:
            return self.max_speed_deg_s
        return min(speed_deg_s, self.max_speed_deg_s)


@dataclass
class ServoReading:
    """Сире зчитування сервопривода: кут вала (raw) і телеметрія."""
    raw_deg: float
    speed_deg_s: float = 0.0
    load_pct: float = 0.0
    voltage_v: float = 0.0
    current_a: float = 0.0
    temperature_c: float = 0.0
    moving: bool = False


def wrap360(angle_deg: float) -> float:
    """Звести кут до [0, 360)."""
    a = angle_deg % 360.0
    return 0.0 if a == 360.0 else a


def wrap180(angle_deg: float) -> float:
    """Звести різницю кутів до [-180, 180)."""
    return (angle_deg + 180.0) % 360.0 - 180.0


def shortest_target(current_unwrapped_deg: float, target_deg: float) -> float:
    """Ціль для неперервної осі: найкоротший шлях від поточного (розгорнутого) кута до target (0..360)."""
    delta = wrap180(target_deg - wrap360(current_unwrapped_deg))
    return current_unwrapped_deg + delta


@dataclass
class AxisStatus:
    axis_deg: float = 0.0           # кут осі (для неперервної — зведений до 0..360)
    unwrapped_deg: float = 0.0      # розгорнутий кут (для обліку обертів)
    reading: Optional[ServoReading] = None
    online: bool = False
    overheated: bool = False
    extra: dict = field(default_factory=dict)
