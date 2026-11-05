"""Спільне для екранів: кольори, перетворення кадру в QPixmap, передача Future у потік інтерфейсу."""
from __future__ import annotations

from concurrent.futures import Future
from typing import Callable

import numpy as np
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QFrame, QSizePolicy, QToolButton, QVBoxLayout, QWidget

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


class CollapsibleSection(QWidget):
    """Блок форми, що згортається і розгортається натисканням на заголовок."""
    toggled = pyqtSignal(bool)

    def __init__(self, title: str, expanded: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.title = title
        self.header = QToolButton()
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setStyleSheet("QToolButton { border: none; font-weight: 600; text-align: left; padding: 4px 0; }")
        self.header.clicked.connect(lambda checked: self.set_expanded(checked))
        self.body = QFrame()
        self.body.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self.header)
        lay.addWidget(self.body)
        self.set_note("")
        self.set_expanded(expanded)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, on: bool) -> None:
        self.header.setChecked(on)
        self.header.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.body.setVisible(on)
        self.toggled.emit(on)

    def set_note(self, note: str, color: str = "") -> None:
        """Підказка в заголовку згорнутого блока, напр. кількість помилок."""
        self._note = note
        self.header.setText(self.title + (f"  —  {note}" if note else ""))
        self.header.setStyleSheet("QToolButton { border: none; font-weight: 600; text-align: left; padding: 4px 0;"
                                  + (f" color: {color};" if color else "") + " }")
