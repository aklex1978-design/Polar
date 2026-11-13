"""Головне вікно: меню «Камери» (список, управління; редагування і калібрування — зі списку камер),
«Налаштування» (загальні, виявлення, ROS 2, карта) і «Робота» (карта, камери, режим роботи, журнал).
Рядок стану: файл бази і останні помилки/попередження з журналу. Необроблені помилки — вікном."""
from __future__ import annotations

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QLabel, QMainWindow, QMessageBox, QStackedWidget

from .. import events as ev

from .calibration_screen import CalibrationScreen
from .camera_form import CameraForm
from .cameras_screen import CamerasScreen
from .control_screen import ControlScreen
from .work_screen import WorkScreen
from .cameras_grid import CamerasGridScreen
from .events_screen import EventsScreen
from .map_settings import MapSettingsScreen
from .models_screen import ModelsScreen
from .settings_screens import DetectionSettingsScreen, GeneralSettingsScreen, RosSettingsScreen
from .work_map import WorkMapScreen


RESTART_CODE = 1001          # код виходу «перезапустити з іншою базою»


class MainWindow(QMainWindow):
    error_signal = pyqtSignal(str, str)

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
        self.general = GeneralSettingsScreen(ctx)
        self.detection_settings = DetectionSettingsScreen(ctx)
        self.ros_settings = RosSettingsScreen(ctx)
        self.events = EventsScreen(ctx)
        self.models = ModelsScreen(ctx)
        self.form.models_requested.connect(lambda: self.stack.setCurrentWidget(self.models))
        self.models.changed.connect(lambda: (self.cameras.refresh(), self.form.reload_models()))
        self.general.restart_requested.connect(self.restart)
        self._entering = {self.models: self.models.enter, self.general: self.general.enter, self.detection_settings: self.detection_settings.enter,
                          self.ros_settings: self.ros_settings.enter, self.events: self.events.enter}
        for page in (self.cameras, self.form, self.control, self.calibration, self.work, self.grid,
                     self.map_settings, self.work_map, self.general, self.detection_settings, self.ros_settings,
                     self.events, self.models):
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
                           ("Управління", lambda: self.stack.setCurrentWidget(self.control)),
                           ("Моделі", lambda: self.stack.setCurrentWidget(self.models))):
            act = QAction(text, self)
            act.triggered.connect(slot)
            menu.addAction(act)
        settings = self.menuBar().addMenu("Налаштування")
        for text, page in (("Загальні", self.general), ("Виявлення", self.detection_settings),
                           ("ROS 2", self.ros_settings), ("Карта", self.map_settings)):
            act = QAction(text, self)
            act.triggered.connect(lambda _=False, p=page: self.stack.setCurrentWidget(p))
            settings.addAction(act)
        work = self.menuBar().addMenu("Робота")
        for text, page in (("Карта", self.work_map), ("Камери", self.grid), ("Режим роботи", self.work),
                           ("Журнал", self.events)):
            act = QAction(text, self)
            act.triggered.connect(lambda _=False, p=page: self.stack.setCurrentWidget(p))
            work.addAction(act)
        # рядок стану: база і останні помилки/попередження
        self.db_label = QLabel(f"База: {ctx.settings.db_path()}")
        self.statusBar().addPermanentWidget(self.db_label)
        with ctx.control._db_lock:
            row = ctx.conn.execute("SELECT MAX(id) FROM events").fetchone()
        self._last_event = row[0] or 0
        self.alert_timer = QTimer(self)
        self.alert_timer.timeout.connect(self.poll_alerts)
        self.alert_timer.start(1000)
        self.error_signal.connect(self.show_error)
        self.exit_code = 0
        self.show_cameras()
        self.resize(1180, 860)

    # --- помилки й попередження -------------------------------------------------------------------------
    def poll_alerts(self) -> None:
        with self.ctx.control._db_lock:
            new = ev.query(self.ctx.conn, kinds=list(ev.ALERT_KINDS), after_id=self._last_event, limit=20)
            top = self.ctx.conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
        self._last_event = max(self._last_event, top)
        if new:
            e = new[0]
            cam = f"{e.camera}: " if e.camera and not e.message.startswith(e.camera) else ""
            self.statusBar().showMessage(f"⚠ {e.local_time[-8:]} · {e.kind_text} — {cam}{e.message}", 30000)
            self.statusBar().setStyleSheet("QStatusBar { color: #c62828; }")
            QTimer.singleShot(30000, lambda: self.statusBar().setStyleSheet(""))

    def show_error(self, short: str, details: str) -> None:
        box = QMessageBox(QMessageBox.Icon.Warning, "Помилка", short, QMessageBox.StandardButton.Ok, self)
        box.setInformativeText("Програма працює далі. Подробиці — у «Робота / Журнал» і у файлі polar.log "
                               "поруч із базою.")
        box.setDetailedText(details)
        box.setModal(False)
        box.open()
        self.last_error_box = box

    def restart(self) -> None:
        """Закрити вікно і відкрити програму знову (інша база з «Налаштування / Загальні»)."""
        from PyQt6.QtWidgets import QApplication
        self.exit_code = RESTART_CODE
        self.close()
        QApplication.instance().exit(RESTART_CODE)

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
        if new in self._entering and self._page is not new:
            self._entering[new]()
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
        self.events.timer.stop()
        self.alert_timer.stop()
        self.ctx.maps.shutdown()
        for w in self.grid.snapshot_windows.values():
            w.close()
        self.control.timer.stop()
        self.form.timer.stop()
        self.ctx.shutdown()
        super().closeEvent(event)
