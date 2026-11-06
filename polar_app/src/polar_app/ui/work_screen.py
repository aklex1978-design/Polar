"""«Робота / Режим роботи»: мітка режиму, «Навчання», «Слідкування» і стан камер."""
from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from polar_core.detection import DETECTION, TRACKING

from ..learning_service import RUNNING

BLUE, GREEN, RED, GREY = "#1e66d0", "#2e7d32", "#c62828", "#6b6b6b"


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
        self.track_btn = QPushButton("Слідкування")
        self.track_btn.setCheckable(True)
        self.track_btn.setToolTip("Порівнювати кадри камер з панорамами; при русі режим сам стає «Виявлення»")
        self.track_btn.clicked.connect(self._on_track)
        top.addWidget(self.learn_btn)
        top.addWidget(self.track_btn)
        v.addLayout(top)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Камера", "Стан", "Хід / обʼєкти", "Покриття панорами",
                                              "Затримка (95 %)"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        for c in (0, 1, 3, 4):
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
            started = self.ctx.start_learning()
            self.message.setText("Навчання: " + ", ".join(started) if started
                                 else "Жодна камера не на звʼязку — Навчання не почато.")
        self.refresh()

    def _on_track(self) -> None:
        det = self.ctx.detection
        if det.active:
            det.stop()
            self.message.setText("Слідкування вимкнено.")
        elif self.ctx.learning.is_running():
            self.message.setText("Спершу дочекайтесь завершення Навчання або зупиніть його.")
        else:
            names = det.start()
            self.message.setText("Слідкування: " + ", ".join(names) if names
                                 else "Немає жодної панорами — спершу Навчання.")
        self.refresh()

    def _set(self, r: int, c: int, text: str, color: str = "") -> None:
        it = self.table.item(r, c)
        if it is None:
            it = QTableWidgetItem()
            self.table.setItem(r, c, it)
        it.setText(text)
        if color:
            from PyQt6.QtGui import QColor
            it.setForeground(QColor(color))

    def refresh(self) -> None:
        learning, det = self.ctx.learning, self.ctx.detection
        recs = self.ctx.records()
        self.table.setRowCount(len(recs))
        running = learning.running()
        for r, rec in enumerate(recs):
            st = learning.status(rec.name)
            pano = learning.panorama(rec.name)
            cov = st.coverage if st.state == RUNNING else (pano.coverage() if pano else 0.0)
            dc = det.camera(rec.name)
            if st.state == RUNNING:
                state, detail = st.text, st.message
            elif det.active:
                state = dc.state
                objs = dc.result.objects if dc.result else []
                detail = "; ".join(f"азимут {o.az:.1f}°, кут місця {o.el:.1f}°" for o in objs[:3])
            else:
                state = st.text if st.state != "idle" else ("панорама застаріла" if rec.panorama_stale else
                                                             ("є панорама" if pano else "немає панорами"))
                detail = st.message
            self._set(r, 0, rec.name)
            self._set(r, 1, state)
            self._set(r, 2, detail or "")
            self._set(r, 3, f"{cov * 100:.1f} %")
            lat = dc.latency.percentile() if (det.active and dc.latency) else None
            if lat is None:
                self._set(r, 4, "—")
            else:
                ok = lat <= dc.latency.limit_ms
                self._set(r, 4, f"{lat:.0f} мс з {dc.latency.limit_ms:.0f}" + ("" if ok else " — перевищено!"),
                          "" if ok else RED)
        if running:
            total = sum(learning.status(n).coverage for n in running) / len(running)
            text, color = f"Навчання — покриття {total * 100:.0f} %", BLUE
        elif det.mode.mode == DETECTION:
            text, color = "Виявлення", RED
        elif det.mode.mode == TRACKING:
            text, color = "Слідкування", GREEN
        else:
            text, color = "Очікування", GREY
        self.mode.setText(text)
        self.mode.setStyleSheet(f"color: white; background: {color}; padding: 4px 10px; font-weight: 600")
        self.learn_btn.setChecked(bool(running))
        self.learn_btn.setText("Зупинити Навчання" if running else "Навчання")
        self.track_btn.setChecked(det.active)
        self.track_btn.setText("Вимкнути Слідкування" if det.active else "Слідкування")
