"""Головне вікно: меню «Камери» (список, редагування, калібрування, управління).
Інші меню («Налаштування», «Робота») — на наступних етапах."""
from __future__ import annotations

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QLabel, QMainWindow, QStackedWidget

from .camera_form import CameraForm
from .cameras_screen import CamerasScreen
from .control_screen import ControlScreen


class MainWindow(QMainWindow):
    def __init__(self, ctx, title_suffix: str = "") -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("Polar" + (f" — {title_suffix}" if title_suffix else ""))
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.cameras = CamerasScreen(ctx)
        self.form = CameraForm(ctx)
        self.control = ControlScreen(ctx.control, ctx)
        self.calibration = QLabel("Калібрування камери — етап 3 плану розробки.")
        for page in (self.cameras, self.form, self.control, self.calibration):
            self.stack.addWidget(page)

        self.cameras.edit_requested.connect(self.edit_camera)
        self.cameras.calibrate_requested.connect(lambda _id: self.stack.setCurrentWidget(self.calibration))
        self.cameras.changed.connect(self.control.reload_cameras)
        self.form.closed.connect(self.show_cameras)
        self.form.saved.connect(lambda _id: (self.cameras.refresh(), self.control.reload_cameras()))
        self._page = None
        self.stack.currentChanged.connect(self._page_changed)

        menu = self.menuBar().addMenu("Камери")
        for text, slot in (("Список", self.show_cameras), ("Редагування", lambda: self.edit_camera(None)),
                           ("Калібрування", lambda: self.stack.setCurrentWidget(self.calibration)),
                           ("Управління", lambda: self.stack.setCurrentWidget(self.control))):
            act = QAction(text, self)
            act.triggered.connect(slot)
            menu.addAction(act)
        self.show_cameras()
        self.resize(1180, 860)

    def _page_changed(self, _index: int) -> None:
        """Відеопотік показують лише «Редагування» і «Управління»: при виході — відключитися."""
        new = self.stack.currentWidget()
        if self._page is self.form and new is not self.form:
            self.form.leave()
        if self._page is self.control and new is not self.control:
            self.control.leave()
        if new is self.control and self._page is not self.control:
            self.control._load_form()
            self.control.enter()
        self._page = new

    def show_cameras(self) -> None:
        self.cameras.refresh()
        self.stack.setCurrentWidget(self.cameras)

    def edit_camera(self, camera_id) -> None:
        self.form.open_camera(camera_id)
        self.stack.setCurrentWidget(self.form)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.form.leave()
        self.control.leave()
        self.control.timer.stop()
        self.form.timer.stop()
        self.ctx.shutdown()
        super().closeEvent(event)
