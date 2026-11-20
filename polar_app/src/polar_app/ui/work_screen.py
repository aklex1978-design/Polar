"""«Робота / Режим роботи»: мітка режиму, «Навчання», «Слідкування», відео камер з ймовірними цілями,
стан камер і таблиця виявлених цілей.

На відео ймовірна ціль — пунктирний квадрат: жовтий — підозра (обʼєкт є на кадрах, координат ще немає),
світло-червоний — підтверджена ціль (її бачать дві й більше камери, є координати)."""
from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QGridLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSplitter, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from .calibration_screen import CameraView
from .common import frame_pixmap

from polar_core.detection import DETECTION, TRACKING
from polar_core.patrol import CUE

from ..learning_service import RUNNING

BLUE, GREEN, RED, GREY = "#1e66d0", "#2e7d32", "#c62828", "#6b6b6b"
TARGET_COLUMNS = ["Камера", "Ціль", "Час", "Азимут, °", "Кут місця, °", "Широта, °", "Довгота, °", "Висота, м",
                  "Відстань, м", "Курс, °", "Швидкість, м/с", "Верт. швидкість, м/с", "Прогноз: широта, °",
                  "Прогноз: довгота, °", "Прогноз: висота, м"]
PRED_COL = TARGET_COLUMNS.index("Прогноз: широта, °")
SUSPECT_COLOR = QColor("#ffd60a")     # жовтий — підозра
TARGET_COLOR = QColor("#ff8080")      # світло-червоний — підтверджена ціль
MARK_FRESH_S = 0.5                    # обʼєкти старші за це — не показувати (камера могла зрушити)


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
        self.track_btn.setToolTip("Камери оглядають зону в межах кута і порівнюють кадри з панорамами; рух — камера "
                                 "зупиняється (підозра на ціль) і йде за ціллю; режим сам стає «Виявлення»")
        self.track_btn.clicked.connect(self._on_track)
        top.addWidget(self.learn_btn)
        top.addWidget(self.track_btn)
        v.addLayout(top)
        # відео камер з ймовірними цілями
        self.cams_box = QWidget()
        cv = QVBoxLayout(self.cams_box)
        cv.setContentsMargins(0, 0, 0, 0)
        self.cams_grid = QGridLayout()
        self.cams_grid.setSpacing(4)
        cv.addLayout(self.cams_grid, 1)
        legend = QLabel(f'<span style="color:{SUSPECT_COLOR.name()}">▢ жовтий пунктир</span> — підозра на ціль; '
                        f'<span style="color:{TARGET_COLOR.name()}">▢ світло-червоний</span> — підтверджена ціль '
                        "(бачать дві й більше камери, є координати)")
        cv.addWidget(legend)
        self.views = {}                 # назва камери → CameraView
        self._acquired = []
        tables = QWidget()
        v_tables = QVBoxLayout(tables)
        v_tables.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.cams_box)
        split.addWidget(tables)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setChildrenCollapsible(False)
        v.addWidget(split, 1)
        outer, v = v, v_tables
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
        v.addWidget(QLabel("Виявлені цілі"))
        self.targets = QTableWidget(0, len(TARGET_COLUMNS))
        self.targets.setHorizontalHeaderLabels(TARGET_COLUMNS)
        self.targets.verticalHeader().setVisible(False)
        self.targets.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.targets.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.targets.horizontalHeader().setStretchLastSection(True)
        self.targets.setToolTip("Один рядок — одна камера; камера, що цілі не бачить, — порожній рядок. "
                                "Широта, довгота, висота і відстань — коли ціль бачать дві й більше камери. "
                                "Курс (від півночі), швидкість і прогноз — за рухом цілі за останні секунди "
                                "(«Налаштування / Виявлення»).")
        v.addWidget(self.targets, 1)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(300)
        self.video_timer = QTimer(self)                 # відео — частіше за таблиці
        self.video_timer.timeout.connect(self.refresh_views)
        self.video_timer.start(100)
        self.refresh()

    # --- відео камер ---------------------------------------------------------------------------
    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self._sync_views()

    def hideEvent(self, e) -> None:  # noqa: N802
        super().hideEvent(e)
        self._release_all()

    def _release_all(self) -> None:
        video = getattr(self.ctx, "video", None)
        for name in self._acquired:
            video.release(name)
        self._acquired = []

    def _sync_views(self) -> None:
        """Плитки відео — для активних камер; потоки підключені, лише поки екран показано."""
        names = [r.name for r in self.ctx.active_records()]
        if list(self.views) != names:
            for view in self.views.values():
                self.cams_grid.removeWidget(view)
                view.deleteLater()
            self.views = {}
            cols = 1 if len(names) <= 1 else (2 if len(names) <= 4 else 3)
            for i, name in enumerate(names):
                view = CameraView()
                view.interactive = False
                view.setMinimumSize(240, 135)
                self.cams_grid.addWidget(view, i // cols, i % cols)
                self.views[name] = view
        if self.isVisible():
            video = getattr(self.ctx, "video", None)
            for name in names:
                if name not in self._acquired and video is not None and video.acquire(name):
                    self._acquired.append(name)
            for name in [n for n in self._acquired if n not in names]:
                video.release(name)
                self._acquired.remove(name)

    def refresh_views(self) -> None:
        if not self.isVisible():
            return
        self._sync_views()
        det = self.ctx.detection
        now = time.monotonic()
        for name, view in self.views.items():
            sf = self.ctx.video.latest(name)
            if sf is not None:
                view.pixmap = frame_pixmap(sf.image)
                view.frame_size = (sf.image.shape[1], sf.image.shape[0])
            dc = det.camera(name)
            marks = []
            numbers = det.targets.target_numbers(name) if det.active else {}
            lock, lockon = dc.lock, dc.lockon
            locked_track = None
            if det.active and lock is not None and lockon is not None and now - lock.t <= MARK_FRESH_S:
                num = numbers.get(lockon.track)
                locked_track = lockon.track
                marks.append((lock.u, lock.v, lock.w, lock.h, TARGET_COLOR if num is not None else SUSPECT_COLOR,
                              (f"ціль №{num}" if num is not None else "підозра") + " · стеження"))
            if det.active and dc.marks and now - dc.frame_t <= MARK_FRESH_S:
                for track, o in dc.marks:
                    if locked_track is not None and track == locked_track:
                        continue                                  # ця — вже позначена як ціль стеження
                    num = numbers.get(track)
                    if num is not None:
                        marks.append((o.u, o.v, o.w, o.h, TARGET_COLOR, f"ціль №{num}"))
                    else:
                        marks.append((o.u, o.v, o.w, o.h, SUSPECT_COLOR, "підозра"))
            marks = marks[:max(1, det.settings.max_objects)]
            view.marks = marks
            st = dc.patrol_text if det.active else ""
            view.text_lines = [name] + ([st] if st else [])
            view.update()

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
        elif c == 2:
            from PyQt6.QtCore import Qt
            it.setData(Qt.ItemDataRole.ForegroundRole, None)          # колір теми (підозра минула)

    def _cell(self, table, r: int, c: int, text: str) -> None:
        it = table.item(r, c)
        if it is None:
            it = QTableWidgetItem()
            table.setItem(r, c, it)
        if it.text() != text:
            it.setText(text)

    def refresh_targets(self) -> None:
        det = self.ctx.detection
        secs = getattr(det.targets, "predict_s", 10.0)
        for i, name in enumerate(("широта, °", "довгота, °", "висота, м")):
            label = f"Через {secs:g} с: {name}"
            it = self.targets.horizontalHeaderItem(PRED_COL + i)
            if it is not None and it.text() != label:
                it.setText(label)
        rows = det.targets.rows() if det.active else [{"camera": r.name} for r in self.ctx.active_records()]
        self.targets.setRowCount(len(rows))
        for i, row in enumerate(rows):
            t = row.get("time")
            vals = [row["camera"],
                    f"№{row['target']}" if "target" in row else "",
                    t.astimezone().strftime("%H:%M:%S.%f")[:-3] if t else "",
                    f"{row['az']:.2f}" if "az" in row else "",
                    f"{row['el']:.2f}" if "el" in row else "",
                    f"{row['lat']:.6f}" if "lat" in row else "",
                    f"{row['lon']:.6f}" if "lon" in row else "",
                    f"{row['alt']:.1f}" if "alt" in row else "",
                    f"{row['dist']:.1f}" if row.get("dist") is not None else "",
                    f"{row['course']:.0f}" if row.get("course") is not None else
                    ("стоїть" if "speed" in row else ""),
                    f"{row['speed']:.1f}" if "speed" in row else "",
                    f"{row['climb']:+.1f}" if "climb" in row else "",
                    f"{row['pred_lat']:.6f}" if "pred_lat" in row else "",
                    f"{row['pred_lon']:.6f}" if "pred_lon" in row else "",
                    f"{row['pred_alt']:.1f}" if "pred_alt" in row else ""]
            for c, text in enumerate(vals):
                self._cell(self.targets, i, c, text)

    def refresh(self) -> None:
        self.refresh_targets()
        learning, det = self.ctx.learning, self.ctx.detection
        recs = self.ctx.active_records()
        self.table.setRowCount(len(recs))
        running = learning.running()
        for r, rec in enumerate(recs):
            st = learning.status(rec.name)
            pano = learning.panorama(rec.name)
            cov = st.coverage if st.state == RUNNING else (pano.coverage() if pano else 0.0)
            dc = det.camera(rec.name)
            if st.state == RUNNING:
                state, detail = st.text, f"{st.message} · знімків: {st.snapshots}"
            elif det.active:
                state = dc.state
                objs = dc.result.objects if dc.result else []
                detail = "; ".join(f"азимут {o.az:.1f}°, кут місця {o.el:.1f}°" for o in objs[:3])
                if dc.patrol_text:                              # огляд зони / підозра на ціль
                    detail = dc.patrol_text if dc.suspect is not None else \
                        " · ".join(x for x in (dc.patrol_text, detail) if x)
            else:
                state = st.text if st.state != "idle" else ("панорама застаріла" if rec.panorama_stale else
                                                             ("є панорама" if pano else "немає панорами"))
                detail = st.message
            self._set(r, 0, rec.name)
            self._set(r, 1, state)
            self._set(r, 2, detail or "", RED if det.active and dc.suspect is not None else
                      (BLUE if det.active and dc.patrol is not None and dc.patrol.state == CUE else ""))
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
