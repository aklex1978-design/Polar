"""«Налаштування»: «Загальні» (база, шлях до панорам), «Виявлення», «ROS 2».

Кожен екран: поля → «Зберегти» (перевірка, запис у базу, застосування) і «Скасувати зміни»
(повернути збережені значення). Пояснення, що застосовано одразу, а що — після перезапуску, — під
кнопками.
"""
from __future__ import annotations

import datetime as dt
import os

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
                             QWidget)

from polar_core.control import RMW_CHOICES, TRANSPORTS, RosSettings
from polar_core.detection import DetectionSettings

from .common import BAD, GOOD, WARN

DB_FILTER = "База Polar (*.sqlite *.db);;Усі файли (*)"


def _title(text: str) -> QLabel:
    t = QLabel(text)
    t.setStyleSheet("font-size: 15px; font-weight: 600")
    return t


class _Screen(QWidget):
    def __init__(self, ctx, title: str, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.v = QVBoxLayout(self)
        self.v.addWidget(_title(title))
        self.message = QLabel("")
        self.message.setWordWrap(True)

    def _buttons(self) -> None:
        row = QHBoxLayout()
        self.save_btn = QPushButton("Зберегти")
        self.save_btn.clicked.connect(self.save)
        self.revert_btn = QPushButton("Скасувати зміни")
        self.revert_btn.clicked.connect(self.enter)
        row.addWidget(self.save_btn)
        row.addWidget(self.revert_btn)
        row.addStretch(1)
        self.v.addLayout(row)
        self.v.addWidget(self.message)
        self.v.addStretch(1)

    def say(self, text: str, color: str = "") -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {color}" if color else "")

    def enter(self) -> None:  # noqa: D401
        """Показати збережені значення."""

    def save(self) -> None:
        pass


# --- Загальні ------------------------------------------------------------------------------------------

class GeneralSettingsScreen(_Screen):
    restart_requested = pyqtSignal()

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, "Налаштування / Загальні", parent)
        box = QGroupBox("База даних")
        f = QFormLayout(box)
        self.db_file = QLineEdit()
        self.db_file.setReadOnly(True)
        f.addRow("Файл бази даних SQLite", self.db_file)
        row = QHBoxLayout()
        self.open_btn = QPushButton("Відкрити іншу базу…")
        self.open_btn.clicked.connect(lambda: self.choose_database(create=False))
        self.new_btn = QPushButton("Нова база…")
        self.new_btn.clicked.connect(lambda: self.choose_database(create=True))
        self.backup_btn = QPushButton("Резервна копія…")
        self.backup_btn.clicked.connect(self.backup)
        for b in (self.open_btn, self.new_btn, self.backup_btn):
            row.addWidget(b)
        row.addStretch(1)
        f.addRow("", row)
        self.v.addWidget(box)
        files = QGroupBox("Файли")
        g = QFormLayout(files)
        prow = QHBoxLayout()
        self.pano_dir = QLineEdit()
        self.pano_dir.setPlaceholderText("поруч із базою, тека panoramas")
        browse = QPushButton("Огляд…")
        browse.clicked.connect(self._browse_pano)
        prow.addWidget(self.pano_dir, 1)
        prow.addWidget(browse)
        g.addRow("Шлях до панорам", prow)
        self.pano_hint = QLabel("")
        self.pano_hint.setWordWrap(True)
        g.addRow("", self.pano_hint)
        self.v.addWidget(files)
        self._buttons()

    def enter(self) -> None:
        st = self.ctx.settings
        self.db_file.setText(st.db_path())
        self.pano_dir.setText("" if st.panorama_dir_is_default() else st.panorama_dir())
        self._hint()
        self.say("")

    def _hint(self) -> None:
        st = self.ctx.settings
        self.pano_hint.setText(f"Зараз: {st.panorama_dir()}. Там же — знімки Навчання (screenshots); карти — у "
                               f"теці maps поруч. Уже збережені панорами лишаються на своїх місцях.")

    def _browse_pano(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Тека для панорам", self.pano_dir.text() or "")
        if d:
            self.pano_dir.setText(d)

    def save(self) -> None:
        try:
            path = self.ctx.settings.set_panorama_dir(self.pano_dir.text())
        except ValueError as e:
            self.say(f"Шлях до панорам: {e}", BAD)
            return
        self._hint()
        self.say(f"Збережено. Нові панорами пишуться в {path}.", GOOD)

    def choose_database(self, create: bool, path: str = "") -> bool:
        if not path:
            start = os.path.dirname(self.db_file.text())
            if create:
                path, _ = QFileDialog.getSaveFileName(self, "Нова база", os.path.join(start, "polar.sqlite"), DB_FILTER)
            else:
                path, _ = QFileDialog.getOpenFileName(self, "Відкрити базу", start, DB_FILTER)
            if not path:
                return False
        try:
            full = self.ctx.settings.switch_database(path, create=create)
        except ValueError as e:
            self.say(f"База: {e}", BAD)
            return False
        self.say(f"Наступний запуск — з бази {full}.", GOOD)
        ask = QMessageBox.question(self, "Інша база", f"Програма відкриватиме базу\n{full}\n\nПерезапустити зараз?")
        if ask == QMessageBox.StandardButton.Yes:
            self.restart_requested.emit()
        return True

    def backup(self, path: str = "") -> bool:
        if not path:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
            base = os.path.splitext(os.path.basename(self.db_file.text()))[0]
            start = os.path.join(os.path.dirname(self.db_file.text()), f"{base}_{stamp}.sqlite")
            path, _ = QFileDialog.getSaveFileName(self, "Резервна копія бази", start, DB_FILTER)
            if not path:
                return False
        try:
            written = self.ctx.settings.backup(path)
        except OSError as e:
            self.say(f"Резервна копія: {e}", BAD)
            return False
        tail = (" Разом із базою скопійовано ключ паролів (.key) — зберігайте його окремо й надійно."
                if len(written) > 1 else "")
        self.say(f"Резервну копію записано: {written[0]}.{tail}", GOOD)
        return True


# --- Виявлення ----------------------------------------------------------------------------------------------

DETECTION_FIELDS = [
    # поле, підпис, мін, макс, крок, знаків, підказка
    ("min_size_deg", "Мінімальний розмір обʼєкта, °", 0.01, 30, 0.05, 2,
     "Менші обʼєкти — шум; для кожної камери перераховується в пікселі"),
    ("max_range_m", "Максимальна дальність виявлення, м", 1, 100000, 100, 0,
     "Цілі, розраховані далі, відкидаються"),
    ("diff_threshold", "Поріг різниці, 0–255", 0, 255, 1, 0,
     "Наскільки піксель кадру має відрізнятися від панорами, щоб бути рухом"),
    ("reference_update_min", "Частота оновлення еталона, хв (0 — не оновлювати)", 0, 1440, 1, 1,
     "Як часто панорама підлаштовується під освітлення"),
    ("accuracy_deg", "Критерій точності, °", 0.01, 10, 0.05, 2,
     "Допустима похибка розміщення кадру; з ним порівнюється калібрування"),
    ("hold_s", "Час утримання режиму «Виявлення», с", 0, 3600, 1, 1,
     "Скільки чекати після зникнення руху перед поверненням до «Слідкування»"),
    ("k_tolerance", "Коефіцієнт допуску зіставлення k", 0.1, 100, 0.1, 2,
     "Допустиме розходження променів = k × відстань × tg(критерію точності)"),
    ("sync_window_ms", "Вікно синхронізації кадрів, мс (0 — тривалість кадру)", 0, 10000, 5, 0,
     "Кадри двох камер зіставляються, лише якщо їхні часи різняться не більше"),
    ("min_ray_angle_deg", "Мінімальний кут між променями, °", 0, 89, 0.5, 1,
     "При майже паралельних променях дальність неточна"),
    ("confirm_frames", "Кадрів для підтвердження пари", 1, 1000, 1, 0,
     "Пара — одна ціль, лише коли тримається стільки кадрів поспіль"),
]


class DetectionSettingsScreen(_Screen):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, "Налаштування / Виявлення", parent)
        hint = QLabel("Загальні параметри для всіх камер. Затримка виявлення і кадри за секунду задаються "
                      "окремо для кожної камери у формі камери.")
        hint.setWordWrap(True)
        self.v.addWidget(hint)
        box = QGroupBox("Параметри")
        f = QFormLayout(box)
        self.fields = {}
        for name, label, lo, hi, step, dec, tip in DETECTION_FIELDS:
            w = QSpinBox() if dec == 0 and name in ("diff_threshold", "confirm_frames") else QDoubleSpinBox()
            w.setRange(lo, hi)
            if isinstance(w, QDoubleSpinBox):
                w.setDecimals(dec)
                w.setSingleStep(step)
            w.setToolTip(tip)
            f.addRow(label, w)
            self.fields[name] = w
        self.v.addWidget(box)
        self._buttons()

    def enter(self) -> None:
        s = self.ctx.settings.detection()
        for name, w in self.fields.items():
            w.setValue(getattr(s, name))
        self.say("")

    def values(self) -> DetectionSettings:
        s = DetectionSettings()
        for name, w in self.fields.items():
            cur = getattr(s, name)
            setattr(s, name, int(w.value()) if isinstance(cur, int) else float(w.value()))
        return s

    def save(self) -> None:
        try:
            text = self.ctx.settings.save_detection(self.values())
        except ValueError as e:
            self.say(f"Не збережено: {e}", BAD)
            return
        self.say(text[:1].upper() + text[1:] + ".", GOOD)


# --- ROS 2 ---------------------------------------------------------------------------------------------------

class RosSettingsScreen(_Screen):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, "Налаштування / ROS 2", parent)
        hint = QLabel("Звʼязок із Raspberry Pi камер. rosbridge — Pi доступні по мережі за адресою з форми камери "
                      "(WebSocket), ROS 2 на цьому компʼютері не потрібен. ROS 2 DDS — сервер сам є вузлом ROS 2 "
                      "(потрібен ROS 2 Humble); тоді ROS_DOMAIN_ID, DDS, інтерфейс, Discovery Server і SROS2 мають "
                      "збігатися на всіх вузлах. Спосіб звʼязку й параметри DDS застосовуються після перезапуску.")
        hint.setWordWrap(True)
        self.v.addWidget(hint)
        box = QGroupBox("Параметри")
        f = QFormLayout(box)
        self.transport = QComboBox()
        for label, value in TRANSPORTS.items():
            self.transport.addItem(label, value)
        self.transport.currentIndexChanged.connect(lambda *_: self._transport_changed())
        f.addRow("Звʼязок з Pi", self.transport)
        self.bridge_port = QSpinBox()
        self.bridge_port.setRange(1, 65535)
        f.addRow("Порт rosbridge на Pi", self.bridge_port)
        self.domain = QSpinBox()
        self.domain.setRange(0, 101)
        f.addRow("ROS_DOMAIN_ID", self.domain)
        self.rmw = QComboBox()
        for label, value in RMW_CHOICES.items():
            self.rmw.addItem(label, value)
        f.addRow("Реалізація DDS (RMW)", self.rmw)
        self.interface = QComboBox()
        self.interface.setEditable(True)
        self.interface.setToolTip("Порожньо — усі інтерфейси")
        f.addRow("Мережевий інтерфейс", self.interface)
        drow = QHBoxLayout()
        self.discovery_mode = QComboBox()
        self.discovery_mode.addItems(["Multicast", "Discovery Server"])
        self.discovery_mode.currentIndexChanged.connect(lambda i: self.discovery.setEnabled(i == 1))
        self.discovery = QLineEdit()
        self.discovery.setPlaceholderText("адреса:порт, напр. 192.168.1.10:11811")
        drow.addWidget(self.discovery_mode)
        drow.addWidget(self.discovery, 1)
        f.addRow("Виявлення вузлів", drow)
        self.prefix = QLineEdit()
        f.addRow("Префікс топіків", self.prefix)
        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.5, 100)
        self.rate.setDecimals(1)
        f.addRow("Частота стану, Гц", self.rate)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.2, 60)
        self.timeout.setDecimals(1)
        f.addRow("Тайм-аут звʼязку, с", self.timeout)
        srow = QHBoxLayout()
        self.sros = QCheckBox("Увімкнено")
        self.sros.toggled.connect(lambda on: self.keystore.setEnabled(on))
        self.keystore = QLineEdit()
        self.keystore.setPlaceholderText("тека з ключами SROS2")
        kb = QPushButton("Огляд…")
        kb.clicked.connect(self._browse_keys)
        srow.addWidget(self.sros)
        srow.addWidget(self.keystore, 1)
        srow.addWidget(kb)
        f.addRow("Захист SROS2", srow)
        self.v.addWidget(box)
        self._buttons()

    def _transport_changed(self) -> None:
        dds = self.transport.currentData() == "dds"
        self.bridge_port.setEnabled(not dds)
        for w in (self.domain, self.rmw, self.interface, self.discovery_mode, self.sros):
            w.setEnabled(dds)
        self.discovery.setEnabled(dds and self.discovery_mode.currentIndex() == 1)
        self.keystore.setEnabled(dds and self.sros.isChecked())

    def _browse_keys(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Ключі SROS2", self.keystore.text() or "")
        if d:
            self.keystore.setText(d)

    def enter(self) -> None:
        s = self.ctx.settings.ros()
        self.transport.setCurrentIndex(max(0, self.transport.findData(s.transport)))
        self.bridge_port.setValue(s.bridge_port)
        self.domain.setValue(s.domain_id)
        i = self.rmw.findData(s.rmw)
        self.rmw.setCurrentIndex(max(0, i))
        self.interface.clear()
        self.interface.addItem("")
        self.interface.addItems(self.ctx.settings.interfaces())
        self.interface.setCurrentText(s.interface)
        self.discovery_mode.setCurrentIndex(1 if s.discovery_server else 0)
        self.discovery.setText(s.discovery_server)
        self.discovery.setEnabled(bool(s.discovery_server))
        self.prefix.setText(s.topic_prefix)
        self.rate.setValue(s.state_rate_hz)
        self.timeout.setValue(s.link_timeout_s)
        self.sros.setChecked(s.sros_enabled)
        self.keystore.setText(s.sros_keystore)
        self.keystore.setEnabled(s.sros_enabled)
        self._transport_changed()
        self.say("")

    def values(self) -> RosSettings:
        return RosSettings(
            transport=self.transport.currentData(), bridge_port=self.bridge_port.value(),
            domain_id=self.domain.value(), rmw=self.rmw.currentData(), interface=self.interface.currentText().strip(),
            discovery_server=self.discovery.text().strip() if self.discovery_mode.currentIndex() == 1 else "",
            topic_prefix=self.prefix.text().strip(), state_rate_hz=self.rate.value(),
            link_timeout_s=self.timeout.value(), sros_enabled=self.sros.isChecked(),
            sros_keystore=self.keystore.text().strip())

    def save(self) -> None:
        s = self.values()
        if self.discovery_mode.currentIndex() == 1 and ":" not in s.discovery_server:
            self.say("Не збережено: для Discovery Server вкажіть адреса:порт.", BAD)
            return
        try:
            text, pending = self.ctx.settings.save_ros(s)
        except ValueError as e:
            self.say(f"Не збережено: {e}", BAD)
            return
        self.say(text[:1].upper() + text[1:] + "." + (" Перезапустіть програму." if pending else ""),
                 WARN if pending else GOOD)
