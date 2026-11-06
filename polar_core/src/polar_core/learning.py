"""План режиму Навчання (ТЗ, «Робота / Режим роботи»): повне коло 0–360° по азимуту з кроком HFOV / 2,
після кожного кола кут місця більшає на VFOV / 2, від 0° до 90°. Перекриття сусідніх кадрів — 50 %."""
from __future__ import annotations

import math
from typing import List, Tuple


def learning_plan(hfov_deg: float, vfov_deg: float, el_min: float = 0.0, el_max: float = 90.0,
                  az_start: float = 0.0) -> List[Tuple[float, float]]:
    """Положення камери (кут датчика, кут місця) у порядку обходу. Ряди — змійкою, щоб камера не
    поверталась через усе коло на початок кожного ряду. az_start — зсув кроків по азимуту (напр. щоб
    азимути від півночі припадали на кратні 10°)."""
    az_step = hfov_deg / 2.0
    el_step = vfov_deg / 2.0
    n_az = max(1, int(math.ceil(360.0 / az_step - 1e-9)))
    els = []
    e = max(0.0, el_min)
    while True:
        els.append(min(e, el_max))
        if e + vfov_deg / 2.0 >= el_max or e >= el_max:      # верхній кадр уже дістає до межі
            break
        e += el_step
    plan = []
    for row, el in enumerate(els):
        azs = [(az_start + i * 360.0 / n_az) % 360.0 for i in range(n_az)]
        if row % 2:
            azs.reverse()
        plan.extend((az, el) for az in azs)
    return plan


def az_step(hfov_deg: float) -> float:
    """Крок по азимуту: не більше HFOV/2 і рівно ділить коло."""
    return 360.0 / max(1, int(math.ceil(360.0 / (hfov_deg / 2.0) - 1e-9)))


def aligned_start(north_offset_deg: float, hfov_deg: float) -> float:
    """Зсув плану, за якого кроки по азимуту від півночі кратні самому кроку (0°, крок, 2 кроки …)."""
    return (-north_offset_deg) % az_step(hfov_deg)
