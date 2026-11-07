"""Головне вікно: меню «Камери» (список, управління; редагування і калібрування — зі списку камер),
«Налаштування» (карта; решта — на етапі 8) і «Робота» (карта, камери, режим роботи)."""
from __future__ import annotations

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMainWindow, QStackedWidget

from .calibration_screen import CalibrationScreen
from .camera_form import CameraForm
from .cameras_screen import CamerasScreen
from .control_screen import ControlScreen
from .work_screen import WorkScreen
from .cameras_grid import CamerasGridScreen
from .map_settings import MapSettingsScreen
from .work_map import WorkMapScreen


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
        self.calibration = CalibrationScreen(ctx)
        self.work = WorkScreen(ctx)
        self.grid = CamerasGridScreen(ctx)
        self.map_settings = MapSettingsScreen(ctx)
        self.work_map = WorkMapScreen(ctx)
        for page in (self.cameras, self.form, self.control, self.calibration, self.work, self.grid,
                     self.map_settings, self.work_map):
            self.stack.addWidget(page)

        self.cameras.edit_requested.connect(self.edit_camera)
        self.cameras.calibrate_requested.connect(self.calibrate_camera)
        self.calibration.closed.connect(self.show_cameras)
        self.cameras.changed.connect(self.control.reload_cameras)
        self.form.closed.connect(self.show_cameras)
        self.form.saved.connect(lambda _id: (self.cameras.refresh(), self.control.reload_cameras()))
        self._page = None
        self.stack.currentChanged.connect(self._page_changed)

        menu = self.menuBar().addMenu("Камери")
        # «Редагування» і «Калібрування» відкриваються кнопками в рядку камери (і «Додати») зі списку
        for text, slot in (("Список", self.show_cameras),
                           ("Управління", lambda: self.stack.setCurrentWidget(self.control))):
            act = QAction(text, self)
            act.triggered.connect(slot)
            menu.addAction(act)
        settings = self.menuBar().addMenu("Налаштування")
        act = QAction("Карта", self)
        act.triggered.connect(lambda: self.stack.setCurrentWidget(self.map_settings))
        settings.addAction(act)
        work = self.menuBar().addMenu("Робота")
        for text, page in (("Карта", self.work_map), ("Камери", self.grid), ("Режим роботи", self.work)):
            act = QAction(text, self)
            act.triggered.connect(lambda _=False, p=page: self.stack.setCurrentWidget(p))
            work.addAction(act)
        self.show_cameras()
        self.resize(1180, 860)

    def _page_changed(self, _index: int) -> None:
        """Відеопотік показують лише «Редагування» і «Управління»: при виході — відключитися."""
        new = self.stack.currentWidget()
        if self._page is self.form and new is not self.form:
            self.form.leave()
        if self._page is self.calibration and new is not self.calibration:
            self.calibration.leave()
        if self._page is self.grid and new is not self.grid:
            self.grid.leave()
        if new is self.grid and self._page is not self.grid:
            self.grid.enter()
        if new is self.map_settings and self._page is not self.map_settings:
            self.map_settings.enter()
        if new is self.work_map and self._page is not self.work_map:
            self.work_map.enter()
        if self._page is self.control and new is not self.control:
            self.control.leave()
        if new is self.control and self._page is not self.control:
            self.control._load_form()
            self.control.enter()
        self._page = new

    def show_cameras(self) -> None:
        self.cameras.refresh()
        self.stack.setCurrentWidget(self.cameras)

    def calibrate_camera(self, camera_id) -> None:
        self.calibration.open_camera(camera_id)
        self.stack.setCurrentWidget(self.calibration)

    def edit_camera(self, camera_id) -> None:
        self.form.open_camera(camera_id)
        self.stack.setCurrentWidget(self.form)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.form.leave()
        self.control.leave()
        self.calibration.leave()
        self.calibration.timer.stop()
        self.work.timer.stop()
        self.grid.leave()
        self.grid.timer.stop()
        self.map_settings.timer.stop()
        self.work_map.timer.stop()
        self.ctx.maps.cancel()
        for w in self.grid.snapshot_windows.values():
            w.close()
        self.control.timer.stop()
        self.form.timer.stop()
        self.ctx.shutdown()
        super().closeEvent(event)
