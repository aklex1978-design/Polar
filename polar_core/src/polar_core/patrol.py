"""Огляд зони в режимі Слідкування і стеження за підозрілою ціллю.

Камера послідовно обходить положення в дозволених межах (азимут — межі сервопривода, кут місця — смуга огляду
з налаштувань), на кожному стоїть «час огляду» і порівнює кадри з панорамою. Рух → камера зупиняється
(підозра на ціль) і аналізує його; коли обʼєкт підходить до краю кадру — камера повертається на нього, щоб
ціль лишалась у кадрі. Руху немає довше за час утримання — огляд продовжується з того самого положення.

Підозра (азимут і кут місця від півночі) — те, що надалі отримуватимуть інші камери, щоб навестись на ціль
і визначити її координати.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .angles import wrap180, wrap360
from .learning import limit_plan

PATROL = "огляд"
ALERT = "підозра на ціль"

ARRIVE_TOL_DEG = 1.5        # камера дійшла до цілі
MOVE_TIMEOUT_S = 20.0       # не дійшла (межа, заклинило) — далі однаково


def patrol_plan(hfov_deg: float, vfov_deg: float, el_min: float, el_max: float, step_frac: float,
                north_offset_deg: float = 0.0, pan_limits: Optional[Tuple[float, float]] = None
                ) -> List[Tuple[float, float]]:
    """Положення огляду (азимут від півночі, кут місця): ряди змійкою, крок — частка поля зору
    (0,8 — сусідні кадри перекриваються на 20 %). pan_limits — межі кута датчика азимуту (мін < макс)."""
    step_frac = min(1.0, max(0.1, step_frac))
    n_az = max(1, int(math.ceil(360.0 / (hfov_deg * step_frac) - 1e-9)))
    rows = []
    e = max(0.0, el_min)
    while True:                       # верхній ряд — коли верх кадру дістає до el_max
        rows.append(min(e, el_max))
        if e + vfov_deg / 2.0 >= el_max or e >= el_max:
            break
        e += vfov_deg * step_frac
    plan = []
    for i, el in enumerate(rows):
        azs = [k * 360.0 / n_az for k in range(n_az)]
        plan.extend((az, el) for az in (reversed(azs) if i % 2 else azs))
    if pan_limits is not None:
        plan = limit_plan(plan, pan_limits[0], pan_limits[1])
    return [(wrap360(az + north_offset_deg), el) for az, el in plan]


def near_edge(u: float, v: float, width: int, height: int, edge_frac: float) -> bool:
    """Центр обʼєкта ближче до краю кадру, ніж edge_frac його розміру."""
    mx, my = width * edge_frac, height * edge_frac
    return u < mx or u > width - mx or v < my or v > height - my


@dataclass
class Suspect:
    az: float           # від півночі
    el: float
    since: float
    last_seen: float
    follows: int = 0    # скільки разів камера поверталась за ціллю


class Patroller:
    """Стан огляду однієї камери. update() викликається на кожен кадр і повертає нову ціль камери
    (азимут від півночі, кут місця) або None; ціль після обмеження межами передається в commanded()."""

    def __init__(self, plan: Sequence[Tuple[float, float]], dwell_s: float, hold_s: float, edge_frac: float,
                 cyclic: bool = True) -> None:
        self.plan = list(plan)
        self.dwell_s = dwell_s
        self.hold_s = hold_s
        self.edge_frac = edge_frac
        self.cyclic = cyclic            # повне коло — по колу; у межах азимуту — туди й назад
        self.state = PATROL
        self.index = 0
        self._dir = 1
        self.target: Optional[Tuple[float, float]] = None
        self._cmd_t = 0.0
        self._arrived_t: Optional[float] = None
        self.suspect: Optional[Suspect] = None

    # --- допоміжне ---------------------------------------------------------------------
    def _issue(self, target: Tuple[float, float], t: float) -> Tuple[float, float]:
        self.target = target
        self._cmd_t = t
        self._arrived_t = None
        return target

    def commanded(self, target: Tuple[float, float]) -> None:
        """Фактична ціль після обмеження межами камери."""
        self.target = target

    def _arrived(self, t: float, pose: Optional[Tuple[float, float]], still: bool) -> bool:
        if self.target is None:
            return True
        if t - self._cmd_t > MOVE_TIMEOUT_S:
            return still
        if pose is None or not still:
            return False
        return (abs(wrap180(pose[0] - self.target[0])) <= ARRIVE_TOL_DEG
                and abs(pose[1] - self.target[1]) <= ARRIVE_TOL_DEG)

    def _advance(self) -> None:
        n = len(self.plan)
        if n <= 1:
            return
        if self.cyclic:
            self.index = (self.index + 1) % n
            return
        if not 0 <= self.index + self._dir < n:
            self._dir = -self._dir
        self.index += self._dir

    @staticmethod
    def _primary(objects):
        return max(objects, key=lambda o: o.area_px)

    # --- крок ---------------------------------------------------------------------------
    def update(self, t: float, pose: Optional[Tuple[float, float]], still: bool, compared: bool,
               objects: Sequence = (), frame_size: Tuple[int, int] = (0, 0)) -> Optional[Tuple[float, float]]:
        """pose — поточні (азимут від півночі, кут місця) камери; still — камера стоїть і кадри вже не змазані;
        compared — кадр порівняно з панорамою; objects — виявлені обʼєкти (DetectedObject)."""
        if not self.plan:
            return None
        arrived = self._arrived(t, pose, still)
        if arrived and self._arrived_t is None:
            self._arrived_t = t
        seen = list(objects) if (compared and arrived) else []

        if self.state == PATROL:
            if self.target is None:
                return self._issue(self.plan[self.index], t)
            if not arrived:
                return None
            if seen:                                            # рух — зупинитись і аналізувати
                o = self._primary(seen)
                self.state = ALERT
                self.suspect = Suspect(o.az, o.el, t, t)
                return self._follow(o, t, frame_size)
            if t - self._arrived_t >= self.dwell_s:
                self._advance()
                return self._issue(self.plan[self.index], t)
            return None

        # ALERT: камера стоїть на цілі; за краєм кадру — повертається за нею
        if seen:
            o = self._primary(seen)
            s = self.suspect
            s.az, s.el, s.last_seen = o.az, o.el, t
            return self._follow(o, t, frame_size)
        last = self.suspect.last_seen if self.suspect else t
        if arrived and t - max(last, self._arrived_t or t) >= self.hold_s:
            self.state = PATROL                                  # ціль зникла — огляд далі з того ж положення
            self.suspect = None
            return self._issue(self.plan[self.index], t)
        return None

    def _follow(self, o, t: float, frame_size: Tuple[int, int]) -> Optional[Tuple[float, float]]:
        w, h = frame_size
        if w and h and near_edge(o.u, o.v, w, h, self.edge_frac):
            self.suspect.follows += 1
            return self._issue((wrap360(o.az), o.el), t)
        return None

    # --- для екранів ------------------------------------------------------------------------
    def text(self) -> str:
        if self.state == ALERT and self.suspect is not None:
            s = self.suspect
            az = round(s.az % 360.0, 1) % 360.0
            return (f"ПІДОЗРА НА ЦІЛЬ: азимут {az:.1f}°, кут місця {s.el:.1f}°"
                    + (f" · камера йде за ціллю ({s.follows})" if s.follows else ""))
        return f"огляд: положення {self.index + 1} з {len(self.plan)}"

