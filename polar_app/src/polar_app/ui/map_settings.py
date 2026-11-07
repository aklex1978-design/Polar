"""«Налаштування / Карта»: карта з камерами і таблиця рівнів 1–8 — на кожному рівні своя карта.

Рівень 1 — найбільша деталізація, рівень 8 — найменша (показується вся карта). Вибір файлу для
рівня одразу завантажує карту цього рівня (хід — у колонці «Стан»). «+» веде до рівня 1, «−» — до 8.
"""
from __future__ import annotations

import os

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from polar_core import maps as mp

from .common import BAD, WARN
from .map_view import MapView, draw_camera_points

FILTER = "GeoTIFF (*.tif *.tiff);;Усі файли (*)"


def order_warnings(levels):
    """Рівень з меншим номером має бути детальнішим: попередження, якщо карта рівня менш детальна,
    ніж карта рівня з більшим номером."""
    out = {}
    by = sorted(levels, key=lambda lv: lv.level)
    for i, lv in enumerate(by):
        coarser_num = [o for o in by[i + 1:] if o.res < lv.res * 0.999]
        if coarser_num:
            out[lv.level] = (f"менш детальна, ніж рівень {coarser_num[0].level} — рівень 1 має бути "
                             f"найдетальнішим, рівень 8 — найменш детальним")
    return out


class MapToolbar(QWidget):
    """«−», «+», поточний рівень і «До камер» — під картою."""

    def __init__(self, view: MapView, parent=None) -> None:
        super().__init__(parent)
        self.view = view
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.minus = QPushButton("−")
        self.plus = QPushButton("+")
        for b in (self.minus, self.plus):
            b.setFixedWidth(32)
        self.minus.clicked.connect(view.zoom_out)
        self.plus.clicked.connect(view.zoom_in)
        self.label = QLabel("")
        self.center_btn = QPushButton("До камер")
        self.center_btn.setToolTip("Центр — середина між крайніми координатами камер")
        h.addWidget(self.minus)
        h.addWidget(self.plus)
        h.addWidget(self.label, 1)
        h.addWidget(self.center_btn)
        view.level_changed.connect(lambda _l: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        self.label.setText(self.view.level_text)
        self.minus.setEnabled(self.view.can_zoom_out())
        self.plus.setEnabled(self.view.can_zoom_in())


class MapSettingsScreen(QWidget):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.maps = ctx.maps
        v = QVBoxLayout(self)
        title = QLabel("Налаштування / Карта")
        title.setStyleSheet("font-size: 15px; font-weight: 600")
        v.addWidget(title)
        self.view = MapView()
        self.view.overlays.append(draw_camera_points(self.maps.camera_points))
        v.addWidget(self.view, 1)
        self.toolbar = MapToolbar(self.view)
        self.toolbar.center_btn.clicked.connect(self.center)
        v.addWidget(self.toolbar)

        box = QGroupBox("Рівні карти")
        bl = QVBoxLayout(box)
        hint = QLabel("На кожному рівні — своя карта GeoTIFF (EPSG:3857 або широта/довгота). Рівень 1 — "
                      "найбільша деталізація, рівень 8 — найменша (на ньому показується вся карта). Після "
                      "вибору файлу карта рівня завантажується одразу; «+» веде до рівня 1, «−» — до рівня 8.")
        hint.setWordWrap(True)
        bl.addWidget(hint)
        self.table = QTableWidget(mp.LEVELS, 4)
        self.table.setHorizontalHeaderLabels(["Рівень", "Файл GeoTIFF", "", "Стан"])
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.level_edits = {}
        self.cancel_btns = {}
        for i in range(mp.LEVELS):
            lvl = i + 1
            label = {1: "1 — найбільша деталізація", mp.LEVELS: f"{mp.LEVELS} — найменша деталізація"}.get(lvl, str(lvl))
            self.table.setItem(i, 0, QTableWidgetItem(label))
            ed = QLineEdit()
            ed.setPlaceholderText("— немає карти")
            ed.editingFinished.connect(lambda n=lvl: self._edited(n))
            self.table.setCellWidget(i, 1, ed)
            btns = QWidget()
            bh = QHBoxLayout(btns)
            bh.setContentsMargins(0, 0, 0, 0)
            b1, b2, b3 = QPushButton("Огляд…"), QPushButton("Прибрати"), QPushButton("Скасувати")
            b1.clicked.connect(lambda _=False, n=lvl: self.browse(n))
            b2.clicked.connect(lambda _=False, n=lvl: self.remove(n))
            b3.clicked.connect(lambda _=False, n=lvl: self.maps.cancel(n))
            b3.setVisible(False)
            for b in (b1, b2, b3):
                bh.addWidget(b)
            self.table.setCellWidget(i, 2, btns)
            self.table.setItem(i, 3, QTableWidgetItem(""))
            self.level_edits[lvl] = ed
            self.cancel_btns[lvl] = b3
        self.table.setMinimumHeight(8 * 30 + 30)
        bl.addWidget(self.table)
        row = QHBoxLayout()
        self.osm = QCheckBox("Карти з даних OpenStreetMap — показувати підпис «© OpenStreetMap contributors»")
        self.osm.toggled.connect(self._osm_toggled)
        row.addWidget(self.osm)
        row.addStretch(1)
        self.info = QLabel("")
        row.addWidget(self.info)
        bl.addLayout(row)
        v.addWidget(box)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        v.addWidget(self.message)

        self._loading = False
        self._seen_version = self.maps.version
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(200)

    # --- вхід на екран -----------------------------------------------------------------------------------
    def enter(self) -> None:
        s = self.maps.settings()
        self._loading = True
        for lvl, ed in self.level_edits.items():
            ed.setText(s.level_files.get(lvl, ""))
        self.osm.setChecked(s.osm)
        self._loading = False
        # рівні, для яких файл задано, а карти ще немає (напр. після оновлення програми), — завантажити
        loaded = {lv.level for lv in self.maps.levels()}
        for lvl, path in s.level_files.items():
            st = self.maps.status[lvl]
            if lvl not in loaded and not (st.running or st.queued or st.error) and os.path.isfile(path):
                self.maps.load_level(lvl, path)
        self.center()
        self._show_levels()
        self._refresh_table()

    def center(self) -> None:
        c = self.maps.center()
        if c is not None:
            self.view.center_on(*c)

    def _show_levels(self) -> None:
        self.view.set_levels(self.maps.levels())
        self.toolbar.refresh()

    # --- дії ----------------------------------------------------------------------------------------------------
    def browse(self, level: int) -> None:
        ed = self.level_edits[level]
        start = os.path.dirname(ed.text()) if ed.text() else ""
        path, _ = QFileDialog.getOpenFileName(self, f"Карта рівня {level}", start, FILTER)
        if path:
            ed.setText(path)
            self.load(level, path)

    def _edited(self, level: int) -> None:
        if self._loading:
            return
        path = self.level_edits[level].text().strip()
        current = self.maps.settings().level_files.get(level, "")
        if path == current:
            return
        if path:
            self.load(level, path)
        else:
            self.remove(level)

    def load(self, level: int, path: str) -> None:
        info = self.maps.inspect(path)
        if not info.ok:
            self.message.setText(f"Рівень {level}: {info.text}")
            self.message.setStyleSheet(f"color: {BAD}")
            self.table.item(level - 1, 3).setText(info.text)
            return
        self.message.setText(f"Рівень {level}: завантаження {os.path.basename(path)}…")
        self.message.setStyleSheet("")
        self.maps.load_level(level, path)
        self._refresh_table()

    def remove(self, level: int) -> None:
        self._loading = True
        self.level_edits[level].clear()
        self._loading = False
        self.maps.remove_level(level)
        self.message.setText(f"Рівень {level}: карту прибрано.")
        self.message.setStyleSheet("")
        self._show_levels()
        self._refresh_table()

    def _osm_toggled(self, on: bool) -> None:
        if not self._loading:
            self.maps.set_osm(on)

    # --- стан -------------------------------------------------------------------------------------------------
    def _refresh_table(self) -> None:
        levels = {lv.level: lv for lv in self.maps.levels()}
        order = order_warnings(list(levels.values()))
        for lvl in range(1, mp.LEVELS + 1):
            st = self.maps.status[lvl]
            lv = levels.get(lvl)
            item = self.table.item(lvl - 1, 3)
            color = ""
            if st.running:
                text = f"завантаження {st.fraction * 100:.0f} % — {st.text}"
            elif st.queued:
                text = "у черзі"
            elif st.error:
                text, color = f"помилка: {st.error}", BAD
            elif lv is not None:
                text = (f"{lv.width} × {lv.height} пікс., {lv.ground_res():.2f} м/піксель "
                        f"(зум ≈ {lv.zoom:.1f})")
                for w in filter(None, (lv.warning, order.get(lvl))):
                    text += f" — {w}"
                    color = WARN
            else:
                text = st.text if st.text else ""
            item.setText(text)
            item.setToolTip(text)
            item.setForeground(QBrush(QColor(color)) if color else self.palette().text())
            self.cancel_btns[lvl].setVisible(st.running or st.queued)
        free = mp.free_bytes(self.maps.tiles_dir())
        self.info.setText(f"Каталог карт: {self.maps.tiles_dir()} · вільно {free / 1e9:.1f} ГБ")

    def _poll(self) -> None:
        if self.maps.busy():
            self._refresh_table()
        if self.maps.version != self._seen_version:
            self._seen_version = self.maps.version
            done = [lvl for lvl, st in self.maps.status.items() if st.text == "завантажено"]
            errors = [f"рівень {lvl}: {st.error}" for lvl, st in self.maps.status.items() if st.error]
            if errors:
                self.message.setText("Помилка — " + "; ".join(errors))
                self.message.setStyleSheet(f"color: {BAD}")
            elif done and not self.maps.busy():
                self.message.setText("Карту завантажено: рівні " + ", ".join(map(str, done)) + ".")
                self.message.setStyleSheet("")
            self._loading = True
            self.osm.setChecked(self.maps.settings().osm)
            self._loading = False
            self._show_levels()
            self._refresh_table()
