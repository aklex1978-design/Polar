"""Імітатор відеопотоку: кадри з синтетичної сцени за кутами камери, з затримкою як у справжнього потоку."""
from __future__ import annotations

import time
from typing import Callable, Optional, Tuple

import numpy as np

from .camera import SimCamera


class SimVideoCapture:
    """Має той самий інтерфейс, що cv2.VideoCapture (isOpened/read/release/get).

    angles_at(t) → (азимут, кут місця) камери на момент t або None (камера недоступна).
    Кадр, отриманий у момент t, показує сцену на момент t − delay_s.
    """

    def __init__(self, camera: SimCamera, angles_at: Callable[[float], Optional[Tuple[float, float]]],
                 fps: float = 25.0, delay_s: float = 0.12, clock: Callable[[], float] = time.monotonic,
                 realtime: bool = True) -> None:
        self.camera = camera
        self.angles_at = angles_at
        self.period = 1.0 / fps
        self.delay_s = delay_s
        self._clock = clock
        self._realtime = realtime
        self._next = clock()
        self._open = True
        self._last: Optional[Tuple[float, float]] = None

    def isOpened(self) -> bool:  # noqa: N802 — як у OpenCV
        return self._open

    def set(self, *_args) -> bool:
        return True

    def get(self, _prop) -> float:
        return 0.0

    def read(self):
        if not self._open:
            return False, None
        if self._realtime:
            wait = self._next - self._clock()
            if wait > 0:
                time.sleep(wait)
            self._next = max(self._next + self.period, self._clock())
        now = self._clock()
        ang = self.angles_at(now - self.delay_s) or self._last
        if ang is None:
            return False, None
        self._last = ang
        gray = self.camera.capture(ang[0], ang[1], t=now - self.delay_s)
        return True, np.repeat(gray[:, :, None], 3, axis=2)

    def release(self) -> None:
        self._open = False
