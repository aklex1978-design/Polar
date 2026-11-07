"""«Налаштування / Карта»: карта з камерами, основний файл GeoTIFF, масштаб Z, файли рівнів, завантаження."""
from __future__ import annotations

import os

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QCheckBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from polar_core import maps as mp

from .common import BAD, GOOD, WARN
from .map_view import MapView, draw_camera_points

FILTER = "GeoTIFF (*.tif *.tiff);;Усі файли (*)"


class MapToolbar(QWidget):
    """«+», «−» і поточний рівень — під картою."""

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
        self.minus.setEnabled(self.view.level > 1)
        self.plus.setEnabled(self.view.level < mp.LEVELS)


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

        box = QGroupBox("Завантаження карти")
        g = QGridLayout(box)
        g.addWidget(QLabel("Файл"), 0, 0)
        self.main_file = QLineEdit()
        self.main_file.setPlaceholderText("основний файл карти GeoTIFF (.tif)")
        self.main_file.editingFinished.connect(self._changed)
        g.addWidget(self.main_file, 0, 1)
        browse = QPushButton("Огляд…")
        browse.clicked.connect(lambda: self._browse(self.main_file))
        g.addWidget(browse, 0, 2)
        self.main_info = QLabel("")
        self.main_info.setWordWrap(True)
        g.addWidget(self.main_info, 1, 1, 1, 2)
        g.addWidget(QLabel("Масштаб Z"), 2, 0)
        zrow = QHBoxLayout()
        self.base_zoom = QSpinBox()
        self.base_zoom.setRange(1, 22)
        self.base_zoom.setToolTip("Базовий рівень зуму Z; зберігаються рівні Z…Z+7, кожен наступний удвічі детальніший")
        self.base_zoom.valueChanged.connect(self._changed)
        self.auto_zoom = QCheckBox("за файлом")
        self.auto_zoom.setToolTip("Z — за роздільною здатністю найдрібнішого (оглядового) файлу")
        self.auto_zoom.toggled.connect(self._changed)
        self.osm = QCheckBox("Карта з даних OpenStreetMap (підпис «© OpenStreetMap contributors»)")
        self.osm.setTristate(False)
        self.osm.toggled.connect(self._changed)
        zrow.addWidget(self.base_zoom)
        zrow.addWidget(self.auto_zoom)
        zrow.addSpacing(16)
        zrow.addWidget(self.osm)
        zrow.addStretch(1)
        g.addLayout(zrow, 2, 1, 1, 2)
        v.addWidget(box)

        lv = QGroupBox("Окремі файли для рівнів")
        lvl = QVBoxLayout(lv)
        self.table = QTableWidget(mp.LEVELS, 4)
        self.table.setHorizontalHeaderLabels(["Рівень", "Файл GeoTIFF", "", "Стан"])
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.level_edits = {}
        for i in range(mp.LEVELS):
            lvl_no = i + 1
            it = QTableWidgetItem("")
            self.table.setItem(i, 0, it)
            ed = QLineEdit()
            ed.setPlaceholderText("— (будується з інших файлів)")
            ed.editingFinished.connect(self._changed)
            self.table.setCellWidget(i, 1, ed)
            btns = QWidget()
            bh = QHBoxLayout(btns)
            bh.setContentsMargins(0, 0, 0, 0)
            b1, b2 = QPushButton("Огляд…"), QPushButton("Прибрати")
            b1.clicked.connect(lambda _=False, e=ed: self._browse(e))
            b2.clicked.connect(lambda _=False, e=ed: (e.clear(), self._changed()))
            bh.addWidget(b1)
            bh.addWidget(b2)
            self.table.setCellWidget(i, 2, btns)
            self.table.setItem(i, 3, QTableWidgetItem(""))
            self.level_edits[lvl_no] = ed
        self.table.setMinimumHeight(8 * 30 + 30)
        lvl.addWidget(self.table)
        v.addWidget(lv)

        row = QHBoxLayout()
        self.info = QLabel("")
        self.info.setWordWrap(True)
        row.addWidget(self.info, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        row.addWidget(self.progress)
        self.load_btn = QPushButton("Завантажити карту")
        self.load_btn.clicked.connect(self.load)
        self.cancel_btn = QPushButton("Скасувати")
        self.cancel_btn.clicked.connect(self.maps.cancel)
        self.cancel_btn.setVisible(False)
        row.addWidget(self.load_btn)
        row.addWidget(self.cancel_btn)
        v.addLayout(row)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        v.addWidget(self.message)

        self._loading = False
        self._seen_version = self.maps.version          # завершення, яких екран ще не показав
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(200)

    # --- вхід на екран ----------------------------------------------------------------------------------
    def enter(self) -> None:
        s = self.maps.settings()
        self._loading = True
        self.main_file.setText(s.main_file)
        self.auto_zoom.setChecked(s.base_zoom is None)
        for lvl, ed in self.level_edits.items():
            ed.setText(s.level_files.get(lvl, ""))
        self._loading = False
        self._show_levels()
        self.center()
        self._changed(save=False)

    def center(self) -> None:
        c = self.maps.center()
        if c is not None:
            self.view.center_on(*c)

    def _show_levels(self) -> None:
        levels = self.maps.levels()
        self.view.set_levels(levels, self.base_zoom.value())
        self.toolbar.refresh()

    # --- зміни ------------------------------------------------------------------------------------------------
    def _browse(self, edit: QLineEdit) -> None:
        start = os.path.dirname(edit.text()) if edit.text() else ""
        path, _ = QFileDialog.getOpenFileName(self, "Файл GeoTIFF", start, FILTER)
        if path:
            edit.setText(path)
            self._changed()

    def _settings_from_form(self) -> mp.MapSettings:
        files = {lvl: ed.text().strip() for lvl, ed in self.level_edits.items() if ed.text().strip()}
        return mp.MapSettings(self.main_file.text().strip(),
                              None if self.auto_zoom.isChecked() else self.base_zoom.value(), files,
                              self.osm.isChecked() if self._osm_touched else None)

    _osm_touched = False

    def _changed(self, *_a, save: bool = True) -> None:
        if self._loading:
            return
        sender = self.sender()
        if sender is self.osm:
            self._osm_touched = True
        s = self._settings_from_form()
        if save:
            self.maps.save_settings(s)
        z, plans, errors = self.maps.plan(s)
        self._loading = True
        self.base_zoom.setEnabled(not self.auto_zoom.isChecked())
        if self.auto_zoom.isChecked() or s.base_zoom is None:
            self.base_zoom.setValue(z)
        if not self._osm_touched:
            self.osm.setChecked(s.osm if s.osm is not None else any(
                self.maps.raster(p.source).osm for p in plans if p.source))
        self._loading = False
        info = self.maps.inspect(s.main_file, need_cameras=True)
        self.main_info.setText(info.text)
        self.main_info.setStyleSheet(f"color: {BAD if s.main_file and not info.ok else ('' if 'увага' not in info.text else WARN)}")
        by_level = {p.level: p for p in plans}
        for lvl in range(1, mp.LEVELS + 1):
            r = lvl - 1
            self.table.item(r, 0).setText(f"{lvl} (Z{'' if lvl == 1 else f'+{lvl - 1}'}) · зум {z + lvl - 1}")
            p = by_level.get(lvl)
            ed = self.level_edits[lvl]
            text = p.state_text if p else ""
            if ed.text().strip():
                fi = self.maps.inspect(ed.text().strip(), level_zoom=z + lvl - 1)
                text = (fi.text if not fi.ok else f"{text} · {fi.text}")
            elif p and p.warning:
                text += f" · {p.warning}"
            self.table.item(r, 3).setText(text)
            self.table.item(r, 3).setToolTip(text)
        if errors:
            self.info.setText("; ".join(errors))
            self.info.setStyleSheet(f"color: {BAD}")
        elif plans:
            need = mp.estimate_bytes(plans)
            free = mp.free_bytes(self.maps.tiles_dir())
            self.info.setText(f"Обсяг рівнів на диску — до {need / 1e6:.0f} МБ; вільно {free / 1e9:.1f} ГБ "
                              f"({self.maps.tiles_dir()})")
            self.info.setStyleSheet(f"color: {BAD}" if need > free else "")
        else:
            self.info.setText("Виберіть основний файл карти або файли рівнів.")
            self.info.setStyleSheet("")
        self.load_btn.setEnabled(bool(plans) and not errors and not self.maps.status.running)

    # --- завантаження --------------------------------------------------------------------------------------------
    def load(self) -> None:
        self.maps.save_settings(self._settings_from_form())
        if not self.maps.start_build():
            self.message.setText(self.maps.status.error)
            self.message.setStyleSheet(f"color: {BAD}")
            return
        self.message.setText("Будуються рівні карти…")
        self.message.setStyleSheet("")

    def _poll(self) -> None:
        st = self.maps.status
        self.progress.setVisible(st.running)
        self.cancel_btn.setVisible(st.running)
        if st.running:
            self.load_btn.setEnabled(False)
            self.progress.setValue(int(st.fraction * 1000))
            self.message.setText(f"Будуються рівні карти: {st.fraction * 100:.0f} % — {st.text}")
        if self.maps.version != self._seen_version and not st.running:
            self._seen_version = self.maps.version
            if st.error:
                self.message.setText(f"Помилка: {st.error}")
                self.message.setStyleSheet(f"color: {BAD}")
            else:
                self.message.setText(st.text[:1].upper() + st.text[1:] + ".")
                self.message.setStyleSheet(f"color: {GOOD}" if st.done else "")
            self._show_levels()
            self._changed(save=False)
