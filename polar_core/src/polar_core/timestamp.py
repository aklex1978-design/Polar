"""Затримка відео по етапах: мітка часу Pi в кадрі і зсув годинників Pi ↔ сервер.

Смужка мітки часу — 28 квадратів 8×8 пікселів у лівому верхньому куті кадру (224×8):
маркер «білий, чорний», 24 біти часу Pi в мілісекундах (за модулем 2^24 ≈ 4,7 год, старший біт першим),
маркер «чорний, білий». Pi малює її ffmpeg-фільтром geq (deploy/raspberry/polar-video.sh) лише на час
вимірювання. Квадрати 8×8 і контраст 16/235 переживають стискання H.264.

Зсув годинників: сервер кілька разів запитує час Pi (rosbridge, /rosapi/get_time) і бере відповідь
з найкоротшим колом: зсув = час_Pi − (надіслано + отримано) / 2, похибка — не більше половини кола.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np

BLOCK = 8
BITS = 24
MOD_MS = 1 << BITS
HEAD = (1, 0)
TAIL = (0, 1)
BLOCKS = len(HEAD) + BITS + len(TAIL)
WIDTH, HEIGHT = BLOCKS * BLOCK, BLOCK
WHITE, BLACK = 235, 16

# Яскравість смужки для ffmpeg geq на Pi (X — стовпчик, T — час кадру в секундах; на Pi T = настінний
# годинник завдяки setpts=RTCTIME). Має давати те саме, що draw_strip: перевіряє test_timestamp.
GEQ_LUM = ("if(lt(X,8),235,if(lt(X,16),16,if(gte(X,216),235,if(gte(X,208),16,"
           "if(mod(floor(mod(floor(T*1000),16777216)/pow(2,25-floor(X/8))),2),235,16)))))")


def _pattern(ms: int) -> List[int]:
    v = int(ms) % MOD_MS
    return list(HEAD) + [(v >> (BITS - 1 - i)) & 1 for i in range(BITS)] + list(TAIL)


def draw_strip(img: np.ndarray, ms: float) -> np.ndarray:
    """Намалювати смужку з часом ms у кадрі (на місці). Так само, як фільтр на Pi; для імітатора і тестів."""
    for i, bit in enumerate(_pattern(int(ms))):
        img[0:HEIGHT, i * BLOCK:(i + 1) * BLOCK] = WHITE if bit else BLACK
    return img


def read_strip(img: np.ndarray) -> Optional[int]:
    """Час Pi з кадру (мс за модулем 2^24) або None, якщо смужки немає."""
    if img is None or img.shape[0] < HEIGHT or img.shape[1] < WIDTH:
        return None
    area = img[2:HEIGHT - 2, :WIDTH]
    if area.ndim == 3:
        area = area.mean(axis=2)
    bits = []
    for i in range(BLOCKS):
        block = area[:, i * BLOCK + 2:(i + 1) * BLOCK - 2]
        m = float(block.mean())
        if 70 < m < 185 or float(block.std()) > 40:           # не чіткий квадрат — смужки немає
            return None
        bits.append(1 if m >= 128 else 0)
    if tuple(bits[:2]) != HEAD or tuple(bits[-2:]) != TAIL:
        return None
    v = 0
    for b in bits[2:-2]:
        v = (v << 1) | b
    return v


def unwrap(value_ms: int, near_ms: float) -> float:
    """Повний час з 24 біт: найближче до near_ms значення, що дає value_ms за модулем 2^24."""
    base = near_ms - (near_ms % MOD_MS)
    return min((base + value_ms + k * MOD_MS for k in (-1, 0, 1)), key=lambda c: abs(c - near_ms))


def estimate_offset(samples: Iterable[Tuple[float, float, float]]) -> Tuple[float, float]:
    """(надіслано, час Pi, отримано) у секундах → (зсув Pi − сервер, коло) за найкоротшим колом."""
    best = min(samples, key=lambda s: s[2] - s[0])
    t0, pi, t1 = best
    return pi - (t0 + t1) / 2.0, t1 - t0


@dataclass
class Stat:
    n: int
    median: float
    low: float
    high: float

    def text(self) -> str:
        return f"{self.median:.0f} мс (мін {self.low:.0f} / макс {self.high:.0f}, {self.n} зам.)"


def stat(values_ms: Sequence[float]) -> Optional[Stat]:
    vals = sorted(values_ms)
    if not vals:
        return None
    return Stat(len(vals), median(vals), vals[0], vals[-1])


@dataclass
class StageReport:
    clock_offset_ms: float          # годинник Pi − годинник сервера
    clock_error_ms: float           # ± половина найкоротшого кола запиту часу
    pi_to_polar: Optional[Stat]     # кадр на Pi (мітка) → кадр отримано Polar
    polar_to_screen: Optional[Stat]  # кадр отримано → кадр показано на екрані
    angles: Optional[Stat]          # стан (кути) на Pi → отримано Polar
    frames_seen: int                # кадрів за час вимірювання (з міткою і без)

    def lines(self) -> List[str]:
        out = [f"Годинник Pi − Polar: {self.clock_offset_ms:+.0f} мс (±{self.clock_error_ms:.0f} мс)"]
        out.append("Кадр: Pi → Polar: " + (self.pi_to_polar.text() if self.pi_to_polar else
                                           f"мітки часу в кадрах не знайдено ({self.frames_seen} кадрів)"))
        out.append("Кадр: Polar → екран: " + (self.polar_to_screen.text() if self.polar_to_screen else
                                              "немає (відео не показувалось на екрані)"))
        out.append("Кути: Pi → Polar: " + (self.angles.text() if self.angles else "немає міток часу в стані"))
        if self.pi_to_polar and self.polar_to_screen:
            out.append(f"Разом кадр Pi → екран: {self.pi_to_polar.median + self.polar_to_screen.median:.0f} мс")
        return out
