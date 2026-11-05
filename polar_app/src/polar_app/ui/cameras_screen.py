"""Меню «Камери»: список камер (назва, модель) з кнопками в рядку і «Додати» над списком."""
from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from .common import BAD, WARN


class CamerasScreen(QWidget):
    edit_requested = pyqtSignal(object)        # id камери або None — нова
    calibrate_requested = pyqtSignal(int)
    changed = pyqtSignal()

    def __init__(self, app_ctx, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = app_ctx
        self.confirm: Callable[[str], bool] = self._ask
        v = QVBoxLayout(self)
        title = QLabel("Камери")
        title.setStyleSheet("font-size: 15px; font-weight: 600")
        top = QHBoxLayout()
        top.addWidget(title)
        top.addStretch(1)
        self.add_btn = QPushButton("Додати")
        self.add_btn.clicked.connect(lambda: self.edit_requested.emit(None))
        top.addWidget(self.add_btn)
        v.addLayout(top)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Назва", "Модель", "", "", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.table)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        v.addWidget(self.message)
        self.refresh()

    def refresh(self) -> None:
        recs = self.ctx.video.records()
        self.table.clearContents()           # прибирає й кнопки попереднього заповнення
        self.table.setRowCount(0)
        self.table.setRowCount(len(recs))
        for r, rec in enumerate(recs):
            name = QTableWidgetItem(rec.name)
            if rec.panorama_stale:
                name.setToolTip("панорама застаріла — потрібен новий огляд")
                name.setForeground(QColor(WARN))
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, QTableWidgetItem(rec.model))
            calib = QPushButton("Калібрування")
            calib.clicked.connect(lambda _=False, i=rec.id: self.calibrate_requested.emit(i))
            edit = QPushButton("Редагувати")
            edit.clicked.connect(lambda _=False, i=rec.id: self.edit_requested.emit(i))
            delete = QPushButton("Видалити")
            delete.setStyleSheet(f"color: {BAD}")
            delete.clicked.connect(lambda _=False, i=rec.id, n=rec.name: self.delete_camera(i, n))
            for c, b in ((2, calib), (3, edit), (4, delete)):
                self.table.setCellWidget(r, c, b)
        self.table.resizeRowsToContents()

    def _ask(self, text: str) -> bool:
        return QMessageBox.question(self, "Видалити камеру", text) == QMessageBox.StandardButton.Yes

    def delete_camera(self, camera_id: int, name: str) -> bool:
        if not self.confirm(f"Видалити «{name}» разом з її панорамою та подіями? Це не можна скасувати."):
            return False
        self.ctx.delete_camera(camera_id)
        self.message.setText(f"Камеру «{name}» видалено.")
        self.refresh()
        self.changed.emit()
        return True
