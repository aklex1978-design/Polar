"""«Робота / Журнал»: події з бази — режими, рух, цілі, Навчання, карта, потоки, налаштування, помилки.

Фільтри: камера, тип події, період, текст. Нові події дописуються самі (раз на секунду), експорт —
у CSV для Excel.
"""
from __future__ import annotations

import datetime as dt
import os

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import events as ev
from .common import BAD, GOOD

ALERT_COLOR = "#c62828"


class EventsScreen(QWidget):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        v = QVBoxLayout(self)
        title = QLabel("Робота / Журнал")
        title.setStyleSheet("font-size: 15px; font-weight: 600")
        v.addWidget(title)
        f = QHBoxLayout()
        f.addWidget(QLabel("Камера"))
        self.camera = QComboBox()
        f.addWidget(self.camera)
        f.addWidget(QLabel("Подія"))
        self.kind = QComboBox()
        f.addWidget(self.kind)
        f.addWidget(QLabel("Період"))
        self.period = QComboBox()
        self.period.addItems(list(ev.PERIODS))
        self.period.setCurrentText("Остання доба")
        f.addWidget(self.period)
        self.text = QLineEdit()
        self.text.setPlaceholderText("пошук у тексті")
        f.addWidget(self.text, 1)
        self.auto = QCheckBox("Оновлювати")
        self.auto.setChecked(True)
        f.addWidget(self.auto)
        self.export_btn = QPushButton("Експорт CSV…")
        self.export_btn.clicked.connect(lambda: self.export())
        f.addWidget(self.export_btn)
        v.addLayout(f)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Час", "Камера", "Подія", "Повідомлення"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        hh = self.table.horizontalHeader()
        for c in range(3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table, 1)
        self.message = QLabel("")
        v.addWidget(self.message)
        for w in (self.camera, self.kind, self.period):
            w.currentIndexChanged.connect(lambda _i: self.reload())
        self.text.editingFinished.connect(self.reload)
        self.events = []
        self._loading = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)

    def enter(self) -> None:
        self._loading = True
        cam, kind = self.camera.currentData(), self.kind.currentData()
        self.camera.clear()
        self.camera.addItem("Усі", None)
        self.camera.addItem("Загальні (без камери)", -1)
        for r in self.ctx.records():
            self.camera.addItem(r.name, r.id)
        self.kind.clear()
        self.kind.addItem("Усі", None)
        self.kind.addItem("Лише помилки й попередження", "alerts")
        with self.ctx.control._db_lock:
            present = ev.kinds_present(self.ctx.conn)
        for k in sorted(set(present) | set(ev.EVENT_KINDS), key=lambda k: ev.EVENT_KINDS.get(k, k)):
            self.kind.addItem(ev.EVENT_KINDS.get(k, k), k)
        self.camera.setCurrentIndex(max(0, self.camera.findData(cam)))
        self.kind.setCurrentIndex(max(0, self.kind.findData(kind)))
        self._loading = False
        self.reload()

    def _filters(self) -> dict:
        cam = self.camera.currentData()
        kind = self.kind.currentData()
        return dict(camera_id=cam if (cam is not None and cam >= 0) else None, no_camera=cam == -1,
                    kinds=list(ev.ALERT_KINDS) if kind == "alerts" else ([kind] if kind else None),
                    since_s=ev.PERIODS[self.period.currentText()], text=self.text.text().strip())

    def reload(self) -> None:
        if self._loading:
            return
        with self.ctx.control._db_lock:
            self.events = ev.query(self.ctx.conn, **self._filters())
        self._fill()

    def _tick(self) -> None:
        if not self.isVisible() or not self.auto.isChecked():
            return
        last = self.events[0].id if self.events else 0
        with self.ctx.control._db_lock:
            new = ev.query(self.ctx.conn, after_id=last, **self._filters())
        if new:
            self.events = (new + self.events)[:2000]
            self._fill()

    def _fill(self) -> None:
        self.table.setRowCount(len(self.events))
        for r, e in enumerate(self.events):
            alert = e.kind in ev.ALERT_KINDS
            for c, text in enumerate((e.local_time, e.camera, e.kind_text, e.message)):
                it = QTableWidgetItem(text)
                if alert:
                    it.setForeground(QBrush(QColor(ALERT_COLOR)))
                if c == 3:
                    it.setToolTip(text)
                self.table.setItem(r, c, it)
        more = " (показано останні 2000)" if len(self.events) >= 2000 else ""
        self.message.setText(f"Подій: {len(self.events)}{more}")
        self.message.setStyleSheet("")

    def export(self, path: str = "") -> bool:
        if not path:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
            path, _ = QFileDialog.getSaveFileName(self, "Експорт журналу", os.path.join(
                os.path.dirname(self.ctx.settings.db_path()), f"polar_events_{stamp}.csv"), "CSV (*.csv)")
            if not path:
                return False
        try:
            ev.export_csv(self.events, path)
        except OSError as e:
            self.message.setText(f"Експорт: {e}")
            self.message.setStyleSheet(f"color: {BAD}")
            return False
        self.message.setText(f"Записано {len(self.events)} подій: {path}")
        self.message.setStyleSheet(f"color: {GOOD}")
        return True
