"""«Камери / Моделі»: список моделей камер з характеристиками оптики; додавання, редагування, видалення.

Модель задає оптику камер: кути огляду HFOV/VFOV і дисторсію; довідково — роздільну здатність сенсора і
примітки. У формі камери модель вибирається зі списку, а оптика показується лише для перегляду. Зміна
моделі оновлює всі її камери; зміна кутів огляду позначає їхні панорами застарілими.
"""
from __future__ import annotations

from typing import Dict, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from polar_core.camera_models import CameraModelSpec

from .common import BAD, GOOD, WARN

FIELDS = [
    # поле, підпис, мін, макс, знаків
    ("fov_x_deg", "Кут огляду X (HFOV), ° *", 0.1, 180, 2),
    ("fov_y_deg", "Кут огляду Y (VFOV), ° *", 0.1, 180, 2),
    ("k1", "Дисторсія k1", -10, 10, 5),
    ("k2", "Дисторсія k2", -10, 10, 5),
    ("p1", "Дисторсія p1", -10, 10, 5),
    ("p2", "Дисторсія p2", -10, 10, 5),
    ("k3", "Дисторсія k3", -10, 10, 5),
]


def _opt_int(text: str):
    t = text.strip()
    if not t:
        return None
    try:
        return int(t)
    except ValueError:
        return "error"


class ModelsScreen(QWidget):
    changed = pyqtSignal()

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.models = []
        self.current: Optional[CameraModelSpec] = None
        v = QVBoxLayout(self)
        title = QLabel("Камери / Моделі")
        title.setStyleSheet("font-size: 15px; font-weight: 600")
        v.addWidget(title)
        h = QHBoxLayout()
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Модель", "Кути огляду", "Сенсор", "Камер"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._selected)
        h.addWidget(self.table, 3)

        box = QGroupBox("Модель")
        f = QFormLayout(box)
        self.widgets: Dict[str, QWidget] = {}
        self.errors: Dict[str, QLabel] = {}

        def add(attr, label, w):
            cell = QWidget()
            lay = QVBoxLayout(cell)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(1)
            lay.addWidget(w)
            err = QLabel("")
            err.setStyleSheet(f"color: {BAD}; font-size: 11px")
            err.setVisible(False)
            lay.addWidget(err)
            self.widgets[attr], self.errors[attr] = w, err
            f.addRow(label, cell)

        name = QLineEdit()
        name.setPlaceholderText("напр. Caddx Infra 256 CA, обʼєктив 9,7 мм")
        add("name", "Назва *", name)
        for attr, label, lo, hi, dec in FIELDS:
            w = QDoubleSpinBox()
            w.setRange(lo, hi)
            w.setDecimals(dec)
            add(attr, label, w)
        srow = QWidget()
        sl = QHBoxLayout(srow)
        sl.setContentsMargins(0, 0, 0, 0)
        self.sensor_x, self.sensor_y = QLineEdit(), QLineEdit()
        self.sensor_x.setPlaceholderText("напр. 256")
        self.sensor_y.setPlaceholderText("192")
        sl.addWidget(self.sensor_x)
        sl.addWidget(QLabel("×"))
        sl.addWidget(self.sensor_y)
        add("sensor_x", "Сенсор, пікселів (довідково)", srow)
        notes = QLineEdit()
        notes.setPlaceholderText("тип (тепловізор / денна), обʼєктив, живлення …")
        add("notes", "Примітки", notes)
        for w in list(self.widgets.values()) + [self.sensor_x, self.sensor_y]:
            sig = w.valueChanged if isinstance(w, QDoubleSpinBox) else (
                w.textChanged if isinstance(w, QLineEdit) else None)
            if sig is not None:
                sig.connect(lambda *_: self.validate())
        right = QVBoxLayout()
        right.addWidget(box)
        btns = QHBoxLayout()
        self.new_btn = QPushButton("Нова модель")
        self.new_btn.clicked.connect(self.new_model)
        self.save_btn = QPushButton("Зберегти")
        self.save_btn.clicked.connect(self.save)
        self.delete_btn = QPushButton("Видалити")
        self.delete_btn.clicked.connect(self.delete)
        btns.addWidget(self.new_btn)
        btns.addStretch(1)
        btns.addWidget(self.delete_btn)
        btns.addWidget(self.save_btn)
        right.addLayout(btns)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        right.addWidget(self.message)
        right.addStretch(1)
        h.addLayout(right, 2)
        v.addLayout(h, 1)
        self._loading = False

    # --- список ----------------------------------------------------------------------------------------
    def enter(self, select_id: Optional[int] = None) -> None:
        self.models = self.ctx.camera_models()
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.models))
        for r, m in enumerate(self.models):
            for c, text in enumerate((m.name, m.optics_text, m.sensor_text, str(m.cameras))):
                self.table.setItem(r, c, QTableWidgetItem(text))
        self.table.blockSignals(False)
        keep = select_id if select_id is not None else (self.current.id if self.current else None)
        row = next((i for i, m in enumerate(self.models) if m.id == keep), 0 if self.models else -1)
        if row >= 0:
            self.table.selectRow(row)
            self._show(self.models[row])
        else:
            self.new_model()

    def _selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self._show(self.models[rows[0].row()])

    def _show(self, m: CameraModelSpec) -> None:
        self.current = m
        self._loading = True
        self.widgets["name"].setText(m.name)
        for attr, *_ in FIELDS:
            self.widgets[attr].setValue(getattr(m, attr))
        self.sensor_x.setText("" if m.sensor_x is None else str(m.sensor_x))
        self.sensor_y.setText("" if m.sensor_y is None else str(m.sensor_y))
        self.widgets["notes"].setText(m.notes)
        self._loading = False
        self.delete_btn.setEnabled(m.id is not None)
        self.validate()

    def new_model(self) -> None:
        self.table.clearSelection()
        self._show(CameraModelSpec())
        self.widgets["name"].setFocus()
        self.say("Нова модель: заповніть назву й оптику та натисніть «Зберегти».")

    # --- форма --------------------------------------------------------------------------------------------
    def form_model(self) -> CameraModelSpec:
        m = CameraModelSpec(id=self.current.id if self.current else None)
        m.name = self.widgets["name"].text().strip()
        for attr, *_ in FIELDS:
            setattr(m, attr, float(self.widgets[attr].value()))
        sx, sy = _opt_int(self.sensor_x.text()), _opt_int(self.sensor_y.text())
        self._sensor_bad = "error" in (sx, sy)
        m.sensor_x, m.sensor_y = (None, None) if self._sensor_bad else (sx, sy)
        m.notes = self.widgets["notes"].text().strip()
        return m

    def validate(self) -> Dict[str, str]:
        if self._loading:
            return {}
        errs = self.form_model().validate(self.models)
        if self._sensor_bad:
            errs["sensor_x"] = "цілі числа"
        for attr, lab in self.errors.items():
            lab.setText(errs.get(attr, ""))
            lab.setVisible(attr in errs)
        self.save_btn.setEnabled(not errs)
        return errs

    def save(self) -> bool:
        if self.validate():
            return False
        m = self.form_model()
        try:
            mid, stale = self.ctx.save_camera_model(m)
        except ValueError as e:
            self.say(f"Не збережено: {e}", BAD)
            return False
        self.current = None
        self.enter(select_id=mid)
        self.changed.emit()
        used = self.current.cameras if self.current else 0
        if stale:
            self.say(f"Збережено. Змінились кути огляду — панорами камер {', '.join(stale)} позначено застарілими, "
                     f"потрібне нове Навчання.", WARN)
        else:
            self.say("Збережено." + (f" Оптику оновлено в камерах моделі ({used})." if used else ""), GOOD)
        return True

    def delete(self) -> bool:
        if not self.current or self.current.id is None:
            return False
        try:
            self.ctx.delete_camera_model(self.current.id)
        except ValueError as e:
            self.say(f"Не видалено: {e}", BAD)
            return False
        name = self.current.name
        self.current = None
        self.enter()
        self.changed.emit()
        self.say(f"Модель «{name}» видалено.", GOOD)
        return True

    def say(self, text: str, color: str = "") -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {color}" if color else "")
