"""Координати цілей (ТЗ, «Робота / Режим роботи» → «Розрахунок координат цілі», «Зіставлення цілей»).

Кожна камера дає промінь від себе до цілі: початок Pᵢ — положення камери в місцевих метрах ENU,
напрям dᵢ — з азимуту θᵢ і кута місця φᵢ цілі. Для двох променів ціль — середина відрізка
найближчого зближення; для трьох і більше — точка, найближча до всіх погоджених променів (найменші
квадрати).

Правило Б: два промені — одна ціль, якщо розходження |(P₁+t₁d₁) − (P₂+t₂d₂)| ≤ k · D · tg δ, де D —
середня відстань від камер до цілі, δ — критерій точності, k — коефіцієнт допуску. Також обовʼязково:
t₁ > 0 і t₂ > 0 (ціль перед камерами), різниця часу кадрів ≤ вікна синхронізації, кут між
променями ≥ мінімального, D ≤ максимальної дальності, пара тримається задану кількість кадрів поспіль
(`Confirmation`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, Hashable, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .geo import GeoPoint, LocalFrame

# --- промені ---------------------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class Ray:
    camera: str                  # назва камери
    obj: Hashable                # обʼєкт на цій камері (номер треку)
    origin: np.ndarray           # Pᵢ, ENU, м
    d: np.ndarray                # одиничний напрям
    t: float = 0.0               # час кадру, с
    azimuth_deg: float = 0.0
    elevation_deg: float = 0.0

    @property
    def key(self) -> Tuple[str, Hashable]:
        return self.camera, self.obj


def direction(azimuth_deg: float, elevation_deg: float) -> np.ndarray:
    """dᵢ = (cos φ sin θ, cos φ cos θ, sin φ) — схід, північ, вгору."""
    th, ph = math.radians(azimuth_deg), math.radians(elevation_deg)
    return np.array([math.cos(ph) * math.sin(th), math.cos(ph) * math.cos(th), math.sin(ph)])


def make_ray(frame: LocalFrame, camera_pos: GeoPoint, camera: str, obj: Hashable, azimuth_deg: float,
             elevation_deg: float, t: float = 0.0) -> Ray:
    """Промінь камери в спільній системі frame. Азимут і кут місця — у місцевій системі камери
    (від її півночі й горизонту); на відстанях у кілометри їх напрям точно переводиться в frame."""
    return Ray(camera, obj, frame.to_enu(camera_pos), frame.direction(camera_pos, azimuth_deg, elevation_deg),
               t, azimuth_deg, elevation_deg)


# --- параметри зіставлення ----------------------------------------------------------------------------


@dataclass
class MatchParams:
    accuracy_deg: float = 0.5        # δ — критерій точності
    k: float = 2.0                   # коефіцієнт допуску
    sync_window_s: float = 0.04      # вікно синхронізації кадрів
    min_angle_deg: float = 5.0       # мінімальний кут між променями
    max_range_m: float = 3000.0      # максимальна дальність виявлення
    confirm_frames: int = 3          # кадрів поспіль для підтвердження пари

    @classmethod
    def from_settings(cls, s, fps: float = 25.0) -> "MatchParams":
        """З «Налаштування / Виявлення» (DetectionSettings). Вікно 0 — тривалість кадру 1000 / fps."""
        window_ms = s.sync_window_ms if s.sync_window_ms > 0 else 1000.0 / max(fps, 1e-6)
        return cls(s.accuracy_deg, s.k_tolerance, window_ms / 1000.0, s.min_ray_angle_deg, s.max_range_m,
                   s.confirm_frames)

    def tolerance(self, distance_m: float) -> float:
        """Допуск правила Б на відстані D: k · D · tg δ."""
        return self.k * distance_m * math.tan(math.radians(self.accuracy_deg))


# --- два промені ---------------------------------------------------------------------------------------


@dataclass
class PairCheck:
    ok: bool
    reason: str = ""
    t1: float = math.nan
    t2: float = math.nan
    miss_m: float = math.inf         # розходження променів
    tolerance_m: float = 0.0
    distance_m: float = math.nan     # D — середня відстань до цілі
    angle_deg: float = 0.0
    point: Optional[np.ndarray] = None

    @property
    def ratio(self) -> float:
        """Розходження / допуск — чим менше, тим краща пара."""
        return self.miss_m / self.tolerance_m if self.tolerance_m > 0 else math.inf


def closest_approach(r1: Ray, r2: Ray) -> Optional[Tuple[float, float, np.ndarray, np.ndarray]]:
    """t₁, t₂ і найближчі точки променів за формулою ТЗ; None — промені паралельні."""
    b = float(r1.d @ r2.d)
    den = 1.0 - b * b
    if den < 1e-12:
        return None
    w = r1.origin - r2.origin
    p, q = float(r1.d @ w), float(r2.d @ w)
    t1 = (b * q - p) / den
    t2 = (q - b * p) / den
    return t1, t2, r1.origin + t1 * r1.d, r2.origin + t2 * r2.d


def ray_angle_deg(r1: Ray, r2: Ray) -> float:
    return math.degrees(math.acos(max(-1.0, min(1.0, float(r1.d @ r2.d)))))


def check_pair(r1: Ray, r2: Ray, params: MatchParams) -> PairCheck:
    """Перевірка пари за правилом Б і додатковими умовами (крім підтвердження кадрами)."""
    if r1.camera == r2.camera:
        return PairCheck(False, "одна камера")
    if abs(r1.t - r2.t) > params.sync_window_s + 1e-9:
        return PairCheck(False, "час кадрів поза вікном синхронізації")
    angle = ray_angle_deg(r1, r2)
    if angle < params.min_angle_deg:
        return PairCheck(False, "кут між променями менший за мінімальний", angle_deg=angle)
    ca = closest_approach(r1, r2)
    if ca is None:
        return PairCheck(False, "промені паралельні", angle_deg=angle)
    t1, t2, p1, p2 = ca
    if t1 <= 0 or t2 <= 0:
        return PairCheck(False, "ціль позаду камери", t1, t2, angle_deg=angle)
    point = (p1 + p2) / 2
    dist = (float(np.linalg.norm(point - r1.origin)) + float(np.linalg.norm(point - r2.origin))) / 2
    miss = float(np.linalg.norm(p1 - p2))
    tol = params.tolerance(dist)
    res = PairCheck(False, "", t1, t2, miss, tol, dist, angle, point)
    if dist > params.max_range_m:
        res.reason = "далі за максимальну дальність"
    elif miss > tol:
        res.reason = "розходження більше за допуск"
    else:
        res.ok = True
    return res


# --- три і більше ---------------------------------------------------------------------------------------


def least_squares_point(rays: Sequence[Ray]) -> np.ndarray:
    """Точка, найближча до всіх променів: Σ(I − d dᵀ) T = Σ(I − d dᵀ) P."""
    a = np.zeros((3, 3))
    rhs = np.zeros(3)
    for r in rays:
        m = np.eye(3) - np.outer(r.d, r.d)
        a += m
        rhs += m @ r.origin
    return np.linalg.solve(a, rhs)


def point_ray_distance(point: np.ndarray, r: Ray) -> Tuple[float, float]:
    """Відстань від точки до променя (перпендикуляр) і параметр t проєкції на промінь."""
    v = point - r.origin
    t = float(v @ r.d)
    return float(np.linalg.norm(v - t * r.d)), t


@dataclass
class Target:
    rays: List[Ray]
    point: np.ndarray                         # T, ENU
    geo: GeoPoint
    distances: Dict[str, float]               # Dᵢ — від кожної камери
    error_m: float                            # СКВ відстаней променів до T
    miss_m: float                             # для пари — розходження; для 3+ — найбільша відстань×2
    ratio: float                              # найгірше відношення розходження до допуску
    confirmed: bool = False
    pairs: List[Tuple[Tuple[str, Hashable], Tuple[str, Hashable]]] = field(default_factory=list)

    @property
    def cameras(self) -> List[str]:
        return [r.camera for r in self.rays]

    @property
    def distance_m(self) -> float:
        return sum(self.distances.values()) / len(self.distances)

    @property
    def max_angle_deg(self) -> float:
        """Найбільший кут між променями цілі: чим він менший, тим гірше визначається дальність
        (похибка вздовж променя ≈ бокова / sin кута)."""
        return max((ray_angle_deg(a, b) for a, b in combinations(self.rays, 2)), default=0.0)

    def ray(self, camera: str) -> Optional[Ray]:
        return next((r for r in self.rays if r.camera == camera), None)


def _fits(point: np.ndarray, r: Ray, params: MatchParams) -> Optional[float]:
    """Відношення відстані променя до T до допуску (за правилом Б), якщо ціль перед камерою."""
    off, t = point_ray_distance(point, r)
    if t <= 0:
        return None
    tol = params.tolerance(t) / 2           # промінь до середини: половина допустимого розходження
    return off / tol if tol > 0 else None


def _grow(seed: Tuple[Ray, Ray], pc: PairCheck, pool: Dict[str, List[Ray]], params: MatchParams):
    """Кандидат від пари: додає з інших камер промені, що погоджуються з точкою; уточнює точку."""
    members = {seed[0].camera: seed[0], seed[1].camera: seed[1]}
    point = pc.point
    while True:
        best = None
        for cam, rays in pool.items():
            if cam in members:
                continue
            for r in rays:
                if any(abs(r.t - m.t) > params.sync_window_s + 1e-9 for m in members.values()):
                    continue
                q = _fits(point, r, params)
                if q is not None and q <= 1.0 and (best is None or q < best[0]):
                    best = (q, r)
        if best is None:
            break
        members[best[1].camera] = best[1]
        trial = least_squares_point(list(members.values()))
        # промені, що після уточнення вийшли за допуск, відкидаються (найгірший першим, поки ≥ 3)
        while len(members) > 2:
            worst = max(members.values(), key=lambda m: _fits(trial, m, params) or math.inf)
            q = _fits(trial, worst, params)
            if q is not None and q <= 1.0:
                break
            del members[worst.camera]
            trial = least_squares_point(list(members.values()))
        if best[1].camera not in members:
            break                              # доданий промінь не втримався — більше не росте
        point = trial
    return list(members.values()), point


def _target(rays: List[Ray], point: np.ndarray, frame: LocalFrame, params: MatchParams, pc: PairCheck) -> Target:
    offs = [point_ray_distance(point, r)[0] for r in rays]
    dist = {r.camera: float(np.linalg.norm(point - r.origin)) for r in rays}
    if len(rays) == 2:
        miss, ratio = pc.miss_m, pc.ratio
    else:
        miss = 2 * max(offs)
        ratio = max((_fits(point, r, params) or math.inf) for r in rays)
    pairs = [(a.key, b.key) for a, b in combinations(rays, 2)]
    return Target(rays, point, frame.to_geo(point), dist, float(math.sqrt(sum(o * o for o in offs) / len(offs))),
                  miss, ratio, pairs=pairs)


def solve(rays: Iterable[Ray], frame: LocalFrame, params: MatchParams) -> List[Target]:
    """Зіставлення обʼєктів між камерами і координати цілей.

    Усі пари обʼєктів різних камер перевіряються за правилом Б. Кожна прийнята пара — кандидат, до
    якого додаються промені інших камер, що з ним погоджуються. Перемагає кандидат, з яким
    погоджується найбільше променів; за рівності — з найменшим відношенням розходження до допуску.
    Його промені вибувають (кожен обʼєкт камери — лише в одній цілі), і пошук повторюється.
    """
    pool: Dict[str, List[Ray]] = {}
    for r in rays:
        pool.setdefault(r.camera, []).append(r)
    targets: List[Target] = []
    while True:
        flat = [r for rs in pool.values() for r in rs]
        best = None
        for r1, r2 in combinations(flat, 2):
            if r1.camera == r2.camera:
                continue
            pc = check_pair(r1, r2, params)
            if not pc.ok:
                continue
            members, point = _grow((r1, r2), pc, pool, params)
            if len(members) == 2:
                point = pc.point
            score = (len(members), -pc.ratio)
            if best is None or score > best[0]:
                best = (score, members, point, pc)
        if best is None:
            break
        _score, members, point, pc = best
        t = _target(members, point, frame, params, pc)
        if t.distance_m <= params.max_range_m:
            targets.append(t)
        used = {r.key for r in members}
        pool = {c: [r for r in rs if r.key not in used] for c, rs in pool.items()}
    return targets


# --- підтвердження кадрами ---------------------------------------------------------------------------------


class Confirmation:
    """«Пара тримається задану кількість кадрів поспіль»: лічильник для кожної пари обʼєктів
    (камера, трек) — (камера, трек). Ціль підтверджена, якщо хоч одна її пара набрала N кадрів."""

    def __init__(self, frames: int = 3) -> None:
        self.frames = max(1, int(frames))
        self._count: Dict[frozenset, int] = {}

    def update(self, targets: Sequence[Target]) -> Sequence[Target]:
        seen: Dict[frozenset, int] = {}
        for t in targets:
            for a, b in t.pairs:
                key = frozenset((a, b))
                seen[key] = self._count.get(key, 0) + 1
            t.confirmed = any(seen[frozenset(p)] >= self.frames for p in t.pairs)
        self._count = seen                       # пара, якої в цьому кадрі немає, починає з нуля
        return targets

    def reset(self) -> None:
        self._count = {}
