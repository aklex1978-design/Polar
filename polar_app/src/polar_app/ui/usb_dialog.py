"""Вікно «Підключені USB-камери»: вибір камери і режиму для форми камери."""
from __future__ import annotations

from typing import List, Optional, Set

from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QLabel,
                             QTableWidget, QTableWidgetItem, QVBoxLayout)

from polar_core.usb_cameras import UsbCamera, VideoMode


class UsbCameraDialog(QDialog):
    def __init__(self, cameras: List[UsbCamera], used: Set[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Підключені USB-камери")
        self.cameras = cameras
        self.used = used
        v = QVBoxLayout(self)
        if not cameras:
            hint = QLabel("USB-камер не знайдено. Перевірте, що камеру підключено і її бачить система. "
                          "Камеру, відкриту іншою програмою (Zoom, Teams, браузер), Windows може не віддати — "
                          "закрийте ту програму і натисніть «Отримати список камер» ще раз.")
            hint.setWordWrap(True)
            v.addWidget(hint)
        self.table = QTableWidget(len(cameras), 3)
        self.table.setHorizontalHeaderLabels(["Пристрій", "Назва", "У Polar"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for r, cam in enumerate(cameras):
            self.table.setItem(r, 0, QTableWidgetItem(cam.device))
            item = QTableWidgetItem(cam.name)
            if cam.stable_path:
                item.setToolTip(cam.stable_path)
            self.table.setItem(r, 1, item)
            self.table.setItem(r, 2, QTableWidgetItem(self.used_by(cam) or ""))
        self.table.resizeColumnsToContents()
        self.table.currentCellChanged.connect(lambda *_: self._fill_modes())
        v.addWidget(self.table)
        form = QFormLayout()
        self.mode = QComboBox()
        form.addRow("Режим", self.mode)
        v.addLayout(form)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Вибрати")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Скасувати")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        v.addWidget(self.buttons)
        self.resize(620, 320)
        if cameras:
            free = next((i for i, c in enumerate(cameras) if not self.used_by(c)), 0)
            self.table.selectRow(free)
            self.table.setCurrentCell(free, 0)
        self._fill_modes()

    def used_by(self, cam: UsbCamera) -> Optional[str]:
        for key in (cam.stable_path, cam.device):
            if key and key in self.used:
                return self.used[key] if isinstance(self.used, dict) else "так"
        return None

    def _fill_modes(self) -> None:
        self.mode.clear()
        cam = self.selected_camera()
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(cam is not None)
        if cam is None:
            return
        if not cam.modes:
            self.mode.addItem("камера не повідомила режимів", None)
            return
        best = cam.best_mode()
        for m in sorted(cam.modes, key=lambda m: (-m.width * m.height, -m.fps)):
            self.mode.addItem(str(m), m)
        self.mode.setCurrentIndex(max(0, self.mode.findData(best)))

    def selected_camera(self) -> Optional[UsbCamera]:
        r = self.table.currentRow()
        return self.cameras[r] if 0 <= r < len(self.cameras) else None

    def selected_mode(self) -> Optional[VideoMode]:
        return self.mode.currentData()
