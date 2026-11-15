"""Рух цілі: напрямок, швидкість і прогноз положення.

Оцінка — пряма за найменшими квадратами по положеннях за останні window_s секунд (згладжує шум
тріангуляції і кутів). Для цілі з координатами (дві й більше камер) — у місцевій системі ENU, метри:
курс від півночі, горизонтальна і вертикальна швидкість, точка через заданий інтервал. Для підозри,
яку бачить одна камера, — по кутах: швидкість зміни азимуту і кута місця, кути через інтервал.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Sequence, Tuple

import numpy as np

from .angles import wrap180, wrap360

MIN_SAMPLES = 3
MIN_SPAN_S = 0.5


def fit_velocity(ts: Sequence[float], pts: np.ndarray) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """Пряма p(t) = p0 + v·(t − t_last): (положення на останній момент, швидкість) або None."""
    if len(ts) < MIN_SAMPLES or ts[-1] - ts[0] < MIN_SPAN_S:
        return None
    t = np.asarray(ts, float) - ts[-1]
    a = np.stack([np.ones_like(t), t], axis=1)
    coef, *_ = np.linalg.lstsq(a, np.asarray(pts, float), rcond=None)
    return coef[0], coef[1]


@dataclass
class Motion:
    position: np.ndarray        # ENU, м — згладжене положення на останній момент
    velocity: np.ndarray        # ENU, м/с

    @property
    def speed_mps(self) -> float:
        """Горизонтальна швидкість."""
        return float(math.hypot(self.velocity[0], self.velocity[1]))

    @property
    def climb_mps(self) -> float:
        return float(self.velocity[2])

    @property
    def course_deg(self) -> Optional[float]:
        """Курс від півночі за годинниковою стрілкою; None — ціль (майже) не рухається по горизонталі."""
        if self.speed_mps < 0.3:
            return None
        return wrap360(math.degrees(math.atan2(self.velocity[0], self.velocity[1])))

    def predict(self, dt_s: float) -> np.ndarray:
        return self.position + self.velocity * dt_s


class MotionTrack:
    """Історія положень цілі (ENU) за вікно часу."""

    def __init__(self, window_s: float = 3.0) -> None:
        self.window_s = window_s
        self._h: Deque[Tuple[float, np.ndarray]] = deque()

    def add(self, t: float, enu) -> None:
        if self._h and t <= self._h[-1][0]:          # той самий кадр розвʼязано ще раз — оновити
            self._h.pop()
        self._h.append((t, np.asarray(enu, float)))
        while self._h and t - self._h[0][0] > self.window_s:
            self._h.popleft()

    def motion(self) -> Optional[Motion]:
        if not self._h:
            return None
        fit = fit_velocity([t for t, _ in self._h], np.stack([p for _, p in self._h]))
        return Motion(*fit) if fit else None


@dataclass
class AngularMotion:
    az: float                   # згладжені кути на останній момент (азимут від півночі)
    el: float
    az_rate: float              # °/с, + — праворуч (за годинниковою)
    el_rate: float              # °/с, + — вгору

    def predict(self, dt_s: float) -> Tuple[float, float]:
        return wrap360(self.az + self.az_rate * dt_s), max(-90.0, min(90.0, self.el + self.el_rate * dt_s))

    def direction_text(self) -> str:
        """Куди рухається ціль у кадрі: «праворуч і вгору» тощо."""
        parts = []
        if abs(self.az_rate) >= 0.05:
            parts.append("праворуч" if self.az_rate > 0 else "ліворуч")
        if abs(self.el_rate) >= 0.05:
            parts.append("вгору" if self.el_rate > 0 else "вниз")
        return " і ".join(parts) if parts else "стоїть"


class AngularTrack:
    """Історія кутів підозри (одна камера) за вікно часу; азимут розгортається через 0/360°."""

    def __init__(self, window_s: float = 3.0) -> None:
        self.window_s = window_s
        self._h: Deque[Tuple[float, float, float]] = deque()
        self._unwrapped: Optional[float] = None

    def add(self, t: float, az: float, el: float) -> None:
        if self._h and t <= self._h[-1][0]:
            self._h.pop()
        if self._unwrapped is None:
            self._unwrapped = az
        else:
            self._unwrapped += wrap180(az - self._unwrapped)
        self._h.append((t, self._unwrapped, el))
        while self._h and t - self._h[0][0] > self.window_s:
            self._h.popleft()

    def motion(self) -> Optional[AngularMotion]:
        if not self._h:
            return None
        fit = fit_velocity([t for t, _, _ in self._h], np.array([(a, e) for _, a, e in self._h]))
        if fit is None:
            return None
        (az, el), (daz, del_) = fit
        return AngularMotion(wrap360(float(az)), float(el), float(daz), float(del_))
