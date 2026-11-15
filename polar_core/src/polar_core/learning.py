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
    n_az = max(1, int(math.ceil(360.0 / (hfov_deg / 2.0) - 1e-9)))
    els = elevation_rows(vfov_deg, el_min, el_max)
    plan = []
    for row, el in enumerate(els):
        azs = [(az_start + i * 360.0 / n_az) % 360.0 for i in range(n_az)]
        if row % 2:
            azs.reverse()
        plan.extend((az, el) for az in azs)
    return plan


def limit_plan(plan: List[Tuple[float, float]], az_min: float, az_max: float,
               tol: float = 0.5) -> List[Tuple[float, float]]:
    """План лише в межах азимуту камери (кут датчика, az_min < az_max у 0…360): вузли поза межами
    відкидаються, а на краях кожного ряду додаються вузли рівно на межах — панорама доходить до них.
    Порядок обходу (змійка) зберігається."""
    rows: List[Tuple[float, List[float]]] = []
    for az, el in plan:
        if not rows or rows[-1][0] != el:
            rows.append((el, []))
        rows[-1][1].append(az)
    out = []
    for i, (el, azs) in enumerate(rows):
        inside = sorted(a for a in azs if az_min - tol <= a <= az_max + tol)
        if not inside or inside[0] - az_min > tol:
            inside.insert(0, az_min)
        if inside[-1] < az_max - tol:
            inside.append(az_max)
        if i % 2:
            inside.reverse()
        out.extend((a, el) for a in inside)
    return out


def elevation_rows(vfov_deg: float, el_min: float = 0.0, el_max: float = 90.0) -> List[float]:
    """Ряди кута місця з кроком VFOV/2 від нижньої межі; останній ряд — коли кадр дістає до верхньої."""
    els = []
    e = max(0.0, el_min)
    while True:
        els.append(min(e, el_max))
        if e + vfov_deg / 2.0 >= el_max or e >= el_max:      # верхній кадр уже дістає до межі
            break
        e += vfov_deg / 2.0
    return els


def next_row(rows: List[float], el: float, direction: int, tol: float = 0.5) -> float:
    """Сусідній ряд кута місця вгору (+1) чи вниз (-1) від поточного кута; на краю — крайній ряд."""
    if direction > 0:
        return next((r for r in rows if r > el + tol), rows[-1])
    return next((r for r in reversed(rows) if r < el - tol), rows[0])


def next_azimuth(az_north: float, step: float, direction: int, tol: float = 0.5) -> float:
    """Сусідній вузол по азимуту від півночі (кратний кроку) праворуч (+1) чи ліворуч (-1)."""
    k = az_north / step
    n = math.floor(k + tol / step) + 1 if direction > 0 else math.ceil(k - tol / step) - 1
    return (n * step) % 360.0


def az_step(hfov_deg: float) -> float:
    """Крок по азимуту: не більше HFOV/2 і рівно ділить коло."""
    return 360.0 / max(1, int(math.ceil(360.0 / (hfov_deg / 2.0) - 1e-9)))


def aligned_start(north_offset_deg: float, hfov_deg: float) -> float:
    """Зсув плану, за якого кроки по азимуту від півночі кратні самому кроку (0°, крок, 2 кроки …)."""
    return (-north_offset_deg) % az_step(hfov_deg)
