"""Наведення інших камер на підозру.

Камера A бачить підозру лише як напрям (азимут, кут місця) — відстань невідома. Ціль лежить десь на
промені з A; камера B бачить цей промінь як лінію на небі. Вузли пошуку для B — точки цього променя
на відстанях від min_m до max_m, переведені в азимут і кут місця з положення B; сусідні вузли — не
ближче за крок (частку поля зору B), від ближніх до дальніх. Коли B побачить рух — дві камери дають
координати цілі, і далі всі вільні камери наводяться прямо на неї (вузол «point»).
"""
from __future__ import annotations

import math
from typing import Callable, List, Optional, Tuple

import numpy as np

from .angles import wrap180
from .geo import GeoPoint, LocalFrame, bearing_elevation

MAX_NODES = 40


def angular_distance(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Наближена кутова відстань між напрямами (азимут, кут місця), °."""
    cos_el = math.cos(math.radians((a[1] + b[1]) / 2.0))
    return math.hypot(wrap180(a[0] - b[0]) * cos_el, a[1] - b[1])


def ray_search_nodes(frame: LocalFrame, src: GeoPoint, az: float, el: float, dst: GeoPoint, min_m: float,
                     max_m: float, step_deg: float, allowed: Optional[Callable[[float, float], bool]] = None,
                     samples: int = 240) -> List[Tuple[float, float]]:
    """Вузли пошуку для камери в dst уздовж променя з src (азимут від півночі az, кут місця el).
    allowed(az, el) — чи може камера туди дивитись (межі кута); недоступні точки пропускаються."""
    origin = frame.to_enu(src)
    d = frame.direction(src, az, el)
    min_m = max(1.0, min_m)
    max_m = max(min_m * 1.01, max_m)
    nodes: List[Tuple[float, float]] = []
    last_ok: Optional[Tuple[float, float]] = None
    for r in np.geomspace(min_m, max_m, samples):
        p = frame.to_geo(origin + d * r)
        baz, bel, _dist = bearing_elevation(frame, dst, p)
        if allowed is not None and not allowed(baz, bel):
            continue
        last_ok = (baz, bel)
        if not nodes or angular_distance(nodes[-1], last_ok) >= step_deg:
            nodes.append(last_ok)
    if last_ok is not None and nodes and angular_distance(nodes[-1], last_ok) > step_deg / 2.0:
        nodes.append(last_ok)                      # найдальша точка — теж оглянути
    if len(nodes) > MAX_NODES:                     # дуже довга лінія (B біля променя) — рівномірно проріджено
        idx = np.linspace(0, len(nodes) - 1, MAX_NODES).round().astype(int)
        nodes = [nodes[i] for i in idx]
    return nodes
