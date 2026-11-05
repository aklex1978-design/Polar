"""«Робота / Режим роботи»: мітка режиму і Навчання камер (Слідкування й Виявлення — етап 5)."""
from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from ..learning_service import RUNNING

BLUE, GREY = "#1e66d0", "#6b6b6b"


class WorkScreen(QWidget):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        v = QVBoxLayout(self)
        title = QLabel("Робота / Режим роботи")
        title.setStyleSheet("font-size: 15px; font-weight: 600")
        v.addWidget(title)
        top = QHBoxLayout()
        top.addWidget(QLabel("Режим роботи:"))
        self.mode = QLabel("")
        self.mode.setMinimumWidth(320)
        top.addWidget(self.mode)
        top.addStretch(1)
        self.learn_btn = QPushButton("Навчання")
        self.learn_btn.setCheckable(True)
        self.learn_btn.setToolTip("Камери обходять 360° × 90° і записують панорами; повторне натискання зупиняє")
        self.learn_btn.clicked.connect(self._on_learn)
        top.addWidget(self.learn_btn)
        v.addLayout(top)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Камера", "Стан", "Хід", "Покриття панорами"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        for c in (0, 1, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        v.addWidget(self.message)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(300)
        self.refresh()

    def _on_learn(self) -> None:
        learning = self.ctx.learning
        if learning.is_running():
            learning.stop()
            self.message.setText("Навчання зупиняється…")
        else:
            started = learning.start()
            if started:
                self.message.setText("Навчання: " + ", ".join(started))
            else:
                self.message.setText("Жодна камера не на звʼязку — Навчання не почато.")
        self.refresh()

    def refresh(self) -> None:
        learning = self.ctx.learning
        recs = self.ctx.records()
        self.table.setRowCount(len(recs))
        running = learning.running()
        covs = []
        for r, rec in enumerate(recs):
            st = learning.status(rec.name)
            pano = learning.panorama(rec.name)
            cov = st.coverage if st.state == RUNNING else (pano.coverage() if pano else 0.0)
            covs.append(cov)
            state = st.text if st.state != "idle" else ("панорама застаріла" if rec.panorama_stale else
                                                         ("є панорама" if pano else "немає панорами"))
            items = [rec.name, state, st.message or "", f"{cov * 100:.1f} %"]
            for c, text in enumerate(items):
                it = self.table.item(r, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.table.setItem(r, c, it)
                it.setText(text)
        if running:
            total = sum(learning.status(n).coverage for n in running) / len(running)
            self.mode.setText(f"Навчання — покриття {total * 100:.0f} %")
            self.mode.setStyleSheet(f"color: white; background: {BLUE}; padding: 4px 10px; font-weight: 600")
        else:
            self.mode.setText("Очікування (Слідкування і Виявлення — етап 5)")
            self.mode.setStyleSheet(f"color: white; background: {GREY}; padding: 4px 10px; font-weight: 600")
        self.learn_btn.setChecked(bool(running))
        self.learn_btn.setText("Зупинити Навчання" if running else "Навчання")
