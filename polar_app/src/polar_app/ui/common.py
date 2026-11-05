"""Спільне для екранів: кольори, перетворення кадру в QPixmap, передача Future у потік інтерфейсу."""
from __future__ import annotations

from concurrent.futures import Future
from typing import Callable

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap

GOOD, BAD, WARN, QUIET = "#2e7d32", "#c62828", "#ef6c00", "#6b6b6b"


class FutureBridge(QObject):
    """Результат Future з робочого потоку → виклик у потоці інтерфейсу."""
    done = pyqtSignal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self.done.connect(lambda fn, f: fn(f))

    def watch(self, future: Future, fn: Callable[[Future], None]) -> None:
        future.add_done_callback(lambda f: self.done.emit(fn, f))


def frame_pixmap(image: np.ndarray) -> QPixmap:
    """Кадр OpenCV (BGR або сірий) → QPixmap."""
    if image.ndim == 2:
        img = np.ascontiguousarray(image)
        q = QImage(img.data, img.shape[1], img.shape[0], img.strides[0], QImage.Format.Format_Grayscale8)
    else:
        img = np.ascontiguousarray(image[:, :, ::-1])
        q = QImage(img.data, img.shape[1], img.shape[0], img.strides[0], QImage.Format.Format_RGB888)
    return QPixmap.fromImage(q.copy())
