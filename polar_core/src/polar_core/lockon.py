"""Стеження за обраною ціллю на кожному кадрі — і коли камера рухається.

Виявлення порівнює кадр з панорамою лише коли камера стоїть. Щоб стежити плавно, без зупинок, обрана ціль
(найяскравіша пляма) далі шукається просто на кадрі: у вікні навколо місця, де вона має бути (її кути →
піксель за поточними кутами камери), береться найяскравіша пляма, що виділяється з фону вікна більше за
поріг = max(мінімальний поріг, K × шум вікна) — той самий відносний поріг, що й у виявленні. Її кути — за
моделлю калібрування камери.

Керування — пропорційне: камера наводиться так, щоб ціль опинилась у центрі камери (центр кіл калібрування),
зі швидкістю, пропорційною відхиленню: далеко — швидше, ближче до центру — повільніше, без ривків. Відхилення
менше за допуск — камера не рухається.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Hashable, Optional, Tuple

import numpy as np

from .angles import wrap180, wrap360

GAIN_PER_S = 1.5            # швидкість наведення = відхилення × GAIN (°/с на °)
MIN_SPEED_DEG_S = 1.0
COMMAND_EVERY_S = 0.2       # не частіше — сервоприводам і мережі досить
LEAD_S = 0.3                # упередження на рух цілі


@dataclass
class LockResult:
    u: float                # центр плями на кадрі, пікселі
    v: float
    w: float
    h: float
    az: float               # від півночі
    el: float
    contrast: float         # наскільки пляма яскравіша за фон вікна
    t: float


class LockOn:
    """Супровід однієї плями по кадрах (без панорами)."""

    def __init__(self, az: float, el: float, size_px: float = 4.0, track: Hashable = None) -> None:
        self.az, self.el = az, el
        self.size_px = max(2.0, size_px)
        self.track = track              # номер треку виявлення — той самий ключ для координат цілі
        self.last: Optional[LockResult] = None

    def update(self, img: np.ndarray, model, sensor_az: float, el: float, t: float, k: float = 5.0,
               floor: float = 8.0,
               window_frac: float = 0.08) -> Optional[LockResult]:
        import cv2

        g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = g.shape[:2]
        u0, v0, ok = model.angles_to_pixel(self.az, self.el, sensor_az, el)
        if not ok or not (0 <= u0 < w and 0 <= v0 < h):
            return None
        half = int(max(16, 4 * self.size_px, w * window_frac))
        x0, y0 = max(0, int(u0) - half), max(0, int(v0) - half)
        x1, y1 = min(w, int(u0) + half + 1), min(h, int(v0) + half + 1)
        win = g[y0:y1, x0:x1].astype(np.float32)
        if win.size < 16:
            return None
        from .detection import robust_sigma

        bg = float(np.median(win))
        threshold = max(floor, k * robust_sigma(win))
        peak = float(win.max())
        if peak - bg <= threshold:
            return None
        hot = (win > bg + threshold).astype(np.uint8)
        n, labels, stats, _c = cv2.connectedComponentsWithStats(hot, 8)
        if n <= 1:
            return None
        # пляма, найближча до очікуваного місця, серед яскравих (не менше половини контрасту найяскравішої)
        best, best_d = None, None
        for i in range(1, n):
            m = labels == i
            c = float(win[m].max()) - bg
            if c < (peak - bg) * 0.5 or stats[i][4] < 2:
                continue
            ys, xs = np.nonzero(m)
            d = math.hypot(x0 + xs.mean() - u0, y0 + ys.mean() - v0)
            if best_d is None or d < best_d:
                best, best_d = i, d
        if best is None:
            return None
        m = labels == best
        ys, xs = np.nonzero(m)
        wts = win[m] - bg
        u = x0 + float((xs * wts).sum() / wts.sum())
        v = y0 + float((ys * wts).sum() / wts.sum())
        bw, bh = float(xs.max() - xs.min() + 1), float(ys.max() - ys.min() + 1)
        az, elv = model.pixel_to_angles(u, v, sensor_az, el)
        self.az, self.el = float(az), float(elv)
        self.size_px = max(2.0, 0.7 * self.size_px + 0.3 * max(bw, bh))
        self.last = LockResult(u, v, bw, bh, self.az, self.el, float(wts.mean()), t)
        return self.last


@dataclass
class Aim:
    """Команда наведення: куди (азимут від півночі і кут місця сервоприводів) і з якою швидкістю."""
    az: float
    el: float
    speed: float


class CenterControl:
    """Пропорційне наведення: ціль → у центр камери, швидкість ∝ відхиленню, з допуском і обмеженням частоти."""

    def __init__(self, deadband_deg: float, max_speed: float = 30.0) -> None:
        self.deadband_deg = deadband_deg
        self.max_speed = max_speed
        self._last_cmd_t = -1e9
        self._last: Optional[Tuple[float, float]] = None

    def update(self, t: float, servo: Tuple[float, float], center: Tuple[float, float],
               target: Tuple[float, float], rate: Tuple[float, float] = (0.0, 0.0)) -> Optional[Aim]:
        """servo — поточні кути сервоприводів (азимут від півночі, кут місця); center — куди дивиться центр
        камери; target — кути цілі; rate — швидкість цілі, °/с."""
        err_az = wrap180(target[0] + rate[0] * LEAD_S - center[0])
        err_el = target[1] + rate[1] * LEAD_S - center[1]
        err = math.hypot(err_az * math.cos(math.radians(target[1])), err_el)
        if err <= self.deadband_deg or t - self._last_cmd_t < COMMAND_EVERY_S:
            return None
        aim = (wrap360(servo[0] + err_az), servo[1] + err_el)
        if self._last is not None and abs(wrap180(aim[0] - self._last[0])) < 0.05 and abs(aim[1] - self._last[1]) < 0.05:
            return None
        self._last_cmd_t, self._last = t, aim
        speed = min(self.max_speed, max(MIN_SPEED_DEG_S, GAIN_PER_S * err
                                        + math.hypot(rate[0], rate[1])))
        return Aim(aim[0], aim[1], speed)
