"""Головне вікно. Поки що лише «Камери / Управління»; решта меню — етап 8."""
from __future__ import annotations

from PyQt5.QtWidgets import QAction, QMainWindow

from .control_screen import ControlScreen


class MainWindow(QMainWindow):
    def __init__(self, service, title_suffix: str = "") -> None:
        super().__init__()
        self.service = service
        self.setWindowTitle("Polar" + (f" — {title_suffix}" if title_suffix else ""))
        self.control = ControlScreen(service)
        self.setCentralWidget(self.control)
        menu = self.menuBar().addMenu("Камери")
        act = QAction("Управління", self)
        act.triggered.connect(lambda: self.setCentralWidget(self.control))
        menu.addAction(act)
        self.resize(1100, 820)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.control.timer.stop()
        self.service.shutdown()
        super().closeEvent(event)
