"""Привʼязка кадрів до кутів енкодерів за часовими мітками і вимірювання затримки відео.

Усі часи — time.monotonic() на сервері: кадр позначається в момент отримання, стан камери — в момент
отримання повідомлення state. Відео приходить пізніше за кути (кодування, мережа, буфер), тому
кути для кадру беруться на момент t_кадру − затримка_відео. Затримку вимірює LatencyEstimator.
"""
from __future__ import annotations

import bisect
import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Sequence, Tuple

import numpy as np

from .angles import wrap360


@dataclass
class AngleSample:
    t: float
    azimuth_deg: float      # розгорнутий (без стрибків через 0/360)
    elevation_deg: float
    moving: bool


@dataclass
class FrameAngles:
    azimuth_deg: float      # кут камери 0..360 (без зміщення від півночі)
    elevation_deg: float
    moving: bool
    t_angles: float         # момент, на який взято кути
    gap_s: float            # відстань до найближчого зчитування енкодерів


class AngleHistory:
    """Кільцевий буфер кутів однієї камери з лінійною інтерполяцією за часом."""

    def __init__(self, seconds: float = 30.0, max_gap_s: float = 0.5) -> None:
        self.seconds = seconds
        self.max_gap_s = max_gap_s
        self._s: Deque[AngleSample] = deque()
        self._t: Deque[float] = deque()
        self._lock = threading.Lock()

    def add(self, t: float, azimuth_deg: float, elevation_deg: float, moving: bool) -> None:
        with self._lock:
            if self._s:
                last = self._s[-1]
                if t <= last.t:
                    return                       # повтор або старіше повідомлення
                az = last.azimuth_deg + (((azimuth_deg - last.azimuth_deg) + 180.0) % 360.0 - 180.0)
            else:
                az = azimuth_deg
            self._s.append(AngleSample(t, az, elevation_deg, moving))
            self._t.append(t)
            while self._t and self._t[0] < t - self.seconds:
                self._s.popleft()
                self._t.popleft()

    def __len__(self) -> int:
        return len(self._s)

    def samples(self) -> List[AngleSample]:
        with self._lock:
            return list(self._s)

    def at(self, t: float) -> Optional[FrameAngles]:
        """Кути на момент t; None, якщо поруч немає зчитувань (немає звʼязку з камерою)."""
        with self._lock:
            if not self._s:
                return None
            ts = self._t
            i = bisect.bisect_left(ts, t)
            if i == 0:
                s = self._s[0]
                gap = s.t - t
                return None if gap > self.max_gap_s else FrameAngles(
                    wrap360(s.azimuth_deg), s.elevation_deg, s.moving, t, gap)
            if i == len(ts):
                s = self._s[-1]
                gap = t - s.t
                return None if gap > self.max_gap_s else FrameAngles(
                    wrap360(s.azimuth_deg), s.elevation_deg, s.moving, t, gap)
            a, b = self._s[i - 1], self._s[i]
            gap = min(t - a.t, b.t - t)
            if b.t - a.t > 2 * self.max_gap_s:
                return None
            w = (t - a.t) / (b.t - a.t)
            az = a.azimuth_deg + w * (b.azimuth_deg - a.azimuth_deg)
            el = a.elevation_deg + w * (b.elevation_deg - a.elevation_deg)
            return FrameAngles(wrap360(az), el, a.moving or b.moving, t, gap)


@dataclass
class StampedFrame:
    image: np.ndarray
    t: float                    # отримання кадру
    index: int
    angles: Optional[FrameAngles]

    @property
    def has_angles(self) -> bool:
        return self.angles is not None


def stamp(image, t: float, index: int, history: AngleHistory, video_latency_ms: float) -> StampedFrame:
    return StampedFrame(image, t, index, history.at(t - video_latency_ms / 1000.0))


# --- вимірювання затримки відео ---------------------------------------------------
class LatencyEstimator:
    """Затримка відео відносно енкодерів за рухом камери.

    Під час повороту камери (краще кілька поворотів туди й назад) збираємо:
    * зсув зображення між сусідніми кадрами (фазова кореляція) → кутова швидкість за відео;
    * кутова швидкість за енкодерами.
    Затримка — зсув у часі, за якого ці дві швидкості найкраще збігаються.
    """

    def __init__(self, hfov_deg: float, frame_width: int, max_lag_s: float = 1.5, step_s: float = 0.002,
                 work_width: int = 320) -> None:
        self.deg_per_px = hfov_deg / frame_width
        self.max_lag_s = max_lag_s
        self.step_s = step_s
        self.work_width = work_width
        self._prev: Optional[Tuple[float, np.ndarray]] = None
        self.video: List[Tuple[float, float]] = []      # (t середини, °/с)

    def add_frame(self, image: np.ndarray, t: float) -> None:
        import cv2

        g = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        scale = self.work_width / g.shape[1]
        g = cv2.resize(g, (self.work_width, max(8, int(round(g.shape[0] * scale)))),
                       interpolation=cv2.INTER_AREA).astype(np.float32)
        win = cv2.createHanningWindow((g.shape[1], g.shape[0]), cv2.CV_32F)
        if self._prev is not None:
            t0, g0 = self._prev
            (dx, _dy), _resp = cv2.phaseCorrelate(g0, g, win)
            dt = t - t0
            if dt > 0:
                self.video.append(((t + t0) / 2, -dx / scale * self.deg_per_px / dt))
        self._prev = (t, g)

    def estimate(self, encoder: Sequence[AngleSample]) -> Tuple[float, float]:
        """(затримка, с; якість 0..1 — кореляція). ValueError, якщо руху замало."""
        if len(self.video) < 10 or len(encoder) < 10:
            raise ValueError("замало даних: потрібен поворот камери під час запису")
        et = np.array([s.t for s in encoder])
        ea = np.array([s.azimuth_deg for s in encoder])
        vt = np.array([v[0] for v in self.video])
        vw = np.array([v[1] for v in self.video])
        t0 = max(et[0], vt[0] - self.max_lag_s)
        t1 = min(et[-1], vt[-1])
        if t1 - t0 < 1.0:
            raise ValueError("замало даних: запис коротший за 1 с")
        grid = np.arange(t0, vt[-1], self.step_s)
        pos = np.interp(grid, et, ea)
        ew = np.gradient(pos, self.step_s)
        k = max(1, int(0.04 / self.step_s))                   # згладжування на ~кадр
        ew = np.convolve(ew, np.ones(k) / k, mode="same")
        if np.std(ew) < 1.0:
            raise ValueError("камера майже не рухалась — затримку не виміряти")
        best = (0.0, -2.0)
        lags = np.arange(0.0, self.max_lag_s, self.step_s)
        corr = []
        for lag in lags:
            vgrid = vt - lag                                   # момент руху за енкодерами
            m = (vgrid >= grid[0]) & (vgrid <= grid[-1])
            if m.sum() < 8:
                corr.append(-2.0)
                continue
            e = np.interp(vgrid[m], grid, ew)
            v = vw[m]
            c = _ncc(e, v)
            corr.append(abs(c))                                # знак залежить від напрямку осі
        corr = np.array(corr)
        i = int(np.argmax(corr))
        if 0 < i < len(corr) - 1:                              # уточнення вершини параболою
            y0, y1, y2 = corr[i - 1], corr[i], corr[i + 1]
            den = y0 - 2 * y1 + y2
            frac = 0.5 * (y0 - y2) / den if den else 0.0
        else:
            frac = 0.0
        best = (float(lags[i] + frac * self.step_s), float(corr[i]))
        return best


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    den = math.sqrt(float((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / den) if den else 0.0


# --- контроль затримки -------------------------------------------------------------
class LatencyMonitor:
    """Затримка обробки кадру (від отримання до результату) проти допустимої з параметрів камери."""

    def __init__(self, limit_ms: float, window: int = 200) -> None:
        self.limit_ms = limit_ms
        self._v: Deque[float] = deque(maxlen=window)
        self.frames = 0
        self.frames_without_angles = 0

    def record(self, latency_ms: float, has_angles: bool = True) -> None:
        self._v.append(latency_ms)
        self.frames += 1
        if not has_angles:
            self.frames_without_angles += 1

    def percentile(self, q: float = 95.0) -> Optional[float]:
        return float(np.percentile(list(self._v), q)) if self._v else None

    @property
    def exceeded(self) -> bool:
        p = self.percentile()
        return p is not None and p > self.limit_ms
