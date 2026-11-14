"""Екран «Обʼєкти управління / Управління»: одна форма керування і калібрування камери.

Завжди видно: список камер, живий кадр (з накладками калібрування за бажанням), поточні кути, перехід до цілі,
кроки ▲ ▼ ◀ ▶ і «Стоп». Нижче — вкладки «Сервоприводи», «Калібрування», «Панорама і маска»."""
from __future__ import annotations

import copy
from concurrent.futures import Future
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                             QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea,
                             QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from polar_core.control import (BAUDRATES, AXES, CameraControlConfig, CameraStatus, ManualControlLocked,
                                ServoSettings, ServoTelemetry)

from .calibration_screen import CalibrationPanel, CameraView
from .common import BAD, GOOD, WARN, frame_pixmap
AXIS_TITLE = {"pan": "Горизонтальний (азимут)", "tilt": "Вертикальний (кут місця)"}


def _dot(color: str) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, 10, 10)
    p.end()
    return QIcon(pm)


def _spin(lo: float, hi: float, value: float = 0.0, decimals: int = 1, suffix: str = "",
          step: float = 1.0) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    return s


def _fmt(value: Optional[float], unit: str = "°", digits: int = 1) -> str:
    return "…" if value is None else f"{value:.{digits}f}{unit}"


class _Bridge(QObject):
    """Передає результат Future з робочого потоку в потік інтерфейсу."""
    done = pyqtSignal(object, object)


class ServoColumn:
    """Поля налаштувань одного сервопривода."""

    def __init__(self, axis: str) -> None:
        self.axis = axis
        self.servo_id = QSpinBox()
        self.servo_id.setRange(1, 253)
        self.direction = QComboBox()
        self.direction.addItem("прямий", 1)
        self.direction.addItem("зворотний", -1)
        self.zero = _spin(-3000, 3000, 0, 2)
        self.gear = _spin(0.01, 100, 1, 2, step=0.1)
        self.speed = _spin(1, 360, 180, 0)
        self.accel = QSpinBox()
        self.accel.setRange(0, 254)
        self.torque = _spin(0, 100, 100, 0)
        self.temp = _spin(20, 85, 70, 0)
        self.min_deg = _spin(-360, 360, 0, 1)
        self.max_deg = _spin(-360, 360, 90, 1)
        self.unlimited = QCheckBox("без меж (ковзний контакт)") if axis == "pan" else None
        if self.unlimited:
            self.unlimited.toggled.connect(self._toggle_limits)
        self.tel_pos = QLabel("…")
        self.tel_power = QLabel("…")

    def _toggle_limits(self, on: bool) -> None:
        self.min_deg.setEnabled(not on)
        self.max_deg.setEnabled(not on)

    def limits_widget(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.min_deg)
        h.addWidget(QLabel("–"))
        h.addWidget(self.max_deg)
        h.addStretch(1)
        return w

    def load(self, s: ServoSettings) -> None:
        self.servo_id.setValue(s.servo_id)
        self.direction.setCurrentIndex(0 if s.direction == 1 else 1)
        self.zero.setValue(s.zero_offset_deg)
        self.gear.setValue(s.gear_ratio)
        self.speed.setValue(s.max_speed_deg_s)
        self.accel.setValue(s.acceleration)
        self.torque.setValue(s.torque_limit_pct)
        self.temp.setValue(s.temperature_limit_c)
        no_limits = s.min_deg is None
        self.min_deg.setValue(s.min_deg if s.min_deg is not None else 0.0)
        self.max_deg.setValue(s.max_deg if s.max_deg is not None else 360.0)
        if self.unlimited:
            self.unlimited.setChecked(no_limits)
            self._toggle_limits(no_limits)

    def read(self) -> ServoSettings:
        unlimited = bool(self.unlimited and self.unlimited.isChecked())
        return ServoSettings(
            axis=self.axis, servo_id=self.servo_id.value(), direction=self.direction.currentData(),
            zero_offset_deg=self.zero.value(), gear_ratio=self.gear.value(),
            max_speed_deg_s=self.speed.value(), acceleration=self.accel.value(),
            torque_limit_pct=self.torque.value(), temperature_limit_c=self.temp.value(),
            min_deg=None if unlimited else self.min_deg.value(),
            max_deg=None if unlimited else self.max_deg.value())

    def show_telemetry(self, t: Optional[ServoTelemetry], hot_c: float) -> None:
        if t is None or not t.online:
            self.tel_pos.setText("немає відповіді" if t is not None else "…")
            self.tel_pos.setStyleSheet(f"color: {BAD}" if t is not None else "")
            self.tel_power.setText("…")
            return
        self.tel_pos.setText(f"{t.position_deg:.1f}° · {t.temperature_c:.0f} °C")
        self.tel_pos.setStyleSheet(f"color: {BAD}" if t.temperature_c >= hot_c else "")
        self.tel_power.setText(f"{t.voltage_v:.1f} В · {t.current_a:.2f} А · {t.load_pct:.0f} %")


class ControlScreen(QWidget):
    """Сторінка меню «Обʼєкти управління / Управління»."""

    REFRESH_MS = 100

    def __init__(self, service, ctx=None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.ctx = ctx
        self._bridge = _Bridge()
        self._bridge.done.connect(lambda fn, f: fn(f))
        self._current: Optional[str] = None
        self._items: Dict[str, QListWidgetItem] = {}
        self._active = False                  # екран показано — потік вибраної камери підключено
        self._streaming: Optional[str] = None

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._build_list())
        splitter.addWidget(self._build_details())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([270, 800])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(splitter)

        self._fill_list()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(self.REFRESH_MS)

    # --- побудова --------------------------------------------------------------
    def _build_list(self) -> QWidget:
        box = QGroupBox("Камери")
        v = QVBoxLayout(box)
        self.camera_list = QListWidget()
        self.camera_list.setMinimumWidth(250)
        self.camera_list.currentItemChanged.connect(self._on_select)
        v.addWidget(self.camera_list)
        stop_all = QPushButton("Зупинити всі")
        stop_all.setStyleSheet(f"color: {BAD}; font-weight: 600")
        stop_all.clicked.connect(lambda: [self._watch(f, self._report_stop) for f in self.service.stop_all()])
        v.addWidget(stop_all)
        return box

    def _build_details(self) -> QWidget:
        """Верх (завжди видно): відео і керування; низ: вкладки. Межу між ними можна перетягнути."""
        top = QWidget()
        v = QVBoxLayout(top)
        v.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.title = QLabel("Обʼєкти управління / Управління")
        self.title.setStyleSheet("font-size: 15px; font-weight: 600")
        head.addWidget(self.title)
        head.addStretch(1)
        self.demo_servos = QCheckBox("Демо сервоприводів")
        self.demo_servos.setToolTip("Імітатор замість Raspberry Pi і ROS 2 — лише для цієї камери")
        self.demo_video = QCheckBox("Демо відео")
        self.demo_video.setToolTip("Синтетична сцена замість відеопотоку — лише для цієї камери")
        self.demo_servos.toggled.connect(lambda on: self._on_demo(servos=on))
        self.demo_video.toggled.connect(lambda on: self._on_demo(video=on))
        head.addWidget(self.demo_servos)
        head.addWidget(self.demo_video)
        v.addLayout(head)

        video = QGroupBox("Відео")
        vv = QVBoxLayout(video)
        vv.setContentsMargins(6, 4, 6, 4)
        self.view = CameraView()
        self.view.interactive = False
        self.view.setMinimumSize(400, 225)
        vv.addWidget(self.view, 1)
        vrow = QHBoxLayout()
        self.video_info = QLabel("")
        self.video_info.setWordWrap(True)
        vrow.addWidget(self.video_info, 1)
        self.overlays = QCheckBox("Накладки калібрування")
        self.overlays.setToolTip("Центр камери (перехрестя), кола поля зору, вісь нахилу γ і орієнтири на кадрі")
        self.overlays.toggled.connect(lambda _on: self._overlays_changed())
        vrow.addWidget(self.overlays)
        vv.addLayout(vrow)
        v.addWidget(video, 1)

        # керування: поточні кути, ціль, кроки, Стоп — один блок
        ctl_box = QGroupBox("Керування")
        g = QGridLayout(ctl_box)
        g.setContentsMargins(8, 4, 8, 4)
        cur = QHBoxLayout()
        cur.setSpacing(22)
        self.cur_az, self.cur_el, self.cur_move = QLabel("Азимут: …"), QLabel("Кут місця: …"), QLabel("Рух: …")
        self.link_label = QLabel("")
        for lab in (self.cur_az, self.cur_el, self.cur_move, self.link_label):
            cur.addWidget(lab)
        cur.addStretch(1)
        g.addLayout(cur, 0, 0, 1, 7)
        self.tgt_az = _spin(0, 359.9, 0, 1, "°")
        self.tgt_az.setWrapping(True)
        self.tgt_el = _spin(0, 90, 0, 1, "°")
        self.go_btn = QPushButton("Перейти")
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self._on_go)
        tgt = QHBoxLayout()
        tgt.addWidget(QLabel("Ціль: азимут"))
        tgt.addWidget(self.tgt_az)
        tgt.addWidget(QLabel("кут місця"))
        tgt.addWidget(self.tgt_el)
        tgt.addWidget(self.go_btn)
        g.addLayout(tgt, 1, 0, 1, 1)
        self.btn_up, self.btn_down = QPushButton("▲"), QPushButton("▼")
        self.btn_left, self.btn_right = QPushButton("◀"), QPushButton("▶")
        for b in (self.btn_up, self.btn_down, self.btn_left, self.btn_right):
            b.setFixedSize(38, 30)
        self.btn_up.clicked.connect(lambda: self._on_jog(0, +1))
        self.btn_down.clicked.connect(lambda: self._on_jog(0, -1))
        self.btn_left.clicked.connect(lambda: self._on_jog(-1, 0))
        self.btn_right.clicked.connect(lambda: self._on_jog(+1, 0))
        pad = QGridLayout()
        pad.setSpacing(2)
        pad.addWidget(self.btn_up, 0, 1)
        pad.addWidget(self.btn_left, 1, 0)
        pad.addWidget(self.btn_right, 1, 2)
        pad.addWidget(self.btn_down, 2, 1)
        g.addLayout(pad, 1, 2, 2, 1)
        form = QFormLayout()
        self.step = _spin(0.1, 90, 5, 1, "°")
        self.speed = _spin(1, 360, 60, 0, " °/с")
        form.addRow("Крок", self.step)
        form.addRow("Швидкість", self.speed)
        g.addLayout(form, 1, 3, 2, 1)
        self.stop_btn = QPushButton("Стоп")
        self.stop_btn.setMinimumSize(110, 44)
        self.stop_btn.setStyleSheet(f"QPushButton {{ background: {BAD}; color: white; font-weight: 700;"
                                    " border-radius: 6px }")
        self.stop_btn.clicked.connect(self._on_stop)
        g.addWidget(self.stop_btn, 1, 5, 2, 1)
        g.setColumnMinimumWidth(1, 16)
        g.setColumnMinimumWidth(4, 16)
        g.setColumnStretch(6, 1)
        v.addWidget(ctl_box)
        self.error_label = QLabel("")
        self.error_label.setStyleSheet(f"color: {BAD}")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        v.addWidget(self.error_label)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        v.addWidget(self.message)

        # вкладки
        self.tabs = QTabWidget()
        self.tabs.addTab(self._scroll(self._build_servo_group()), "Сервоприводи")
        self.calib: Optional[CalibrationPanel] = None
        if self.ctx is not None:
            self.calib = CalibrationPanel(self.ctx, self.view, lambda text, color="": self._say_color(text, color))
            self.calib_index = self.tabs.addTab(self._scroll(self.calib.calib_tab), "Калібрування")
            self.pano_index = self.tabs.addTab(self._scroll(self.calib.pano_tab), "Панорама і маска")
        self.tabs.currentChanged.connect(self._on_tab)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setChildrenCollapsible(False)
        self._manual_widgets = [self.tgt_az, self.tgt_el, self.go_btn, self.btn_up, self.btn_down,
                                self.btn_left, self.btn_right]
        return split

    @staticmethod
    def _scroll(w: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(w)
        return area

    def _on_tab(self, index: int) -> None:
        if self.calib is not None and index == self.calib_index and not self.overlays.isChecked():
            self.overlays.setChecked(True)            # калібрування — з накладками на кадрі
        self._overlays_changed()

    def _overlays_changed(self) -> None:
        on = self.overlays.isChecked() and self.calib is not None
        # центр перетягується і орієнтири ставляться лише на вкладці «Калібрування»
        self.view.interactive = on and self.tabs.currentIndex() == getattr(self, "calib_index", -1)
        if not on:
            self.view.model = None
            self.view.text_lines = []
            self.view.landmark_mode = False
        elif self.calib is not None:
            self.view.landmark_mode = self.calib.landmark_btn.isChecked()
        self.view.update()

    def open_calibration(self, camera_id: int) -> None:
        """Кнопка «Калібрування» в рядку списку: вибрати камеру і відкрити вкладку калібрування."""
        name = next((n for n in self.service.camera_names() if self.service.config(n).camera_id == camera_id), None)
        if name is not None:
            self.select_camera(name)
        if self.calib is not None:
            self.tabs.setCurrentIndex(self.calib_index)
            self.overlays.setChecked(True)

    def _build_servo_group(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        bus = QHBoxLayout()
        bus2 = QHBoxLayout()
        self.port = QLineEdit()
        self.port.setPlaceholderText("/dev/serial0")
        self.baud = QComboBox()
        for b in BAUDRATES:
            self.baud.addItem(f"{b}", b)
        self.scan_btn = QPushButton("Знайти сервоприводи")
        self.scan_btn.clicked.connect(self._on_scan)
        self.set_id_btn = QPushButton("Змінити ID…")
        self.set_id_btn.clicked.connect(self._on_set_id)
        bus.addWidget(QLabel("Порт"))
        bus.addWidget(self.port, 1)
        bus.addWidget(QLabel("Швидкість обміну, біт/с"))
        bus.addWidget(self.baud)
        bus2.addWidget(self.scan_btn)
        bus2.addWidget(self.set_id_btn)
        bus2.addStretch(1)
        v.addLayout(bus)
        v.addLayout(bus2)

        self.cols = {axis: ServoColumn(axis) for axis in AXES}
        g = QGridLayout()
        g.addWidget(QLabel(f"<b>{AXIS_TITLE['pan']}</b>"), 0, 1)
        g.addWidget(QLabel(f"<b>{AXIS_TITLE['tilt']}</b>"), 0, 2)
        rows = [("ID на шині (1–253) *", "servo_id"), ("Напрямок обертання", "direction"),
                ("Зміщення нуля, °", "zero"), ("Передавальне число", "gear"),
                ("Найбільша швидкість, °/с", "speed"), ("Прискорення", "accel"),
                ("Обмеження моменту, %", "torque"), ("Межа температури, °C", "temp")]
        for r, (label, attr) in enumerate(rows, start=1):
            g.addWidget(QLabel(label), r, 0)
            for c, axis in enumerate(AXES, start=1):
                g.addWidget(getattr(self.cols[axis], attr), r, c)
        r = len(rows) + 1
        g.addWidget(QLabel("Межі кута, °"), r, 0)
        for c, axis in enumerate(AXES, start=1):
            g.addWidget(self.cols[axis].limits_widget(), r, c)
        r += 1
        g.addWidget(self.cols["pan"].unlimited, r, 1)
        r += 1
        g.addWidget(QLabel("<b>Телеметрія (наживо)</b>"), r, 0)
        r += 1
        g.addWidget(QLabel("Положення · температура"), r, 0)
        g.addWidget(QLabel("Напруга · струм · навантаження"), r + 1, 0)
        for c, axis in enumerate(AXES, start=1):
            g.addWidget(self.cols[axis].tel_pos, r, c)
            g.addWidget(self.cols[axis].tel_power, r + 1, c)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(2, 1)
        v.addLayout(g)

        btns = QHBoxLayout()
        self.zero_btn = QPushButton("Встановити нуль")
        menu = QMenu(self.zero_btn)
        for text, axes in (("Обидві осі", AXES), (AXIS_TITLE["pan"], ("pan",)), (AXIS_TITLE["tilt"], ("tilt",))):
            menu.addAction(text).triggered.connect(lambda _=False, a=axes: self._on_zero(a))
        self.zero_btn.setMenu(menu)
        self.save_btn = QPushButton("Зберегти")
        self.save_btn.clicked.connect(self._on_save)
        self.revert_btn = QPushButton("Скасувати зміни")
        self.revert_btn.clicked.connect(self._load_form)
        btns.addWidget(self.zero_btn)
        btns.addStretch(1)
        btns.addWidget(self.revert_btn)
        btns.addWidget(self.save_btn)
        v.addLayout(btns)
        return box

    # --- список камер -------------------------------------------------------------
    def _fill_list(self) -> None:
        self.camera_list.clear()
        self._items.clear()
        for name in self.service.camera_names():
            item = QListWidgetItem(_dot(BAD), name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.camera_list.addItem(item)
            self._items[name] = item
        if self.camera_list.count():
            self.camera_list.setCurrentRow(0)

    def reload_cameras(self) -> None:
        """Список камер змінився (додано, перейменовано, видалено)."""
        current = self._current
        self._fill_list()
        if current in self._items:
            self.select_camera(current)

    def select_camera(self, name: str) -> None:
        self.camera_list.setCurrentItem(self._items[name])

    # --- відеопотік ----------------------------------------------------------------
    def enter(self) -> None:
        """Вхід в «Управління»: підключити й показати потік вибраної камери."""
        self._active = True
        self._sync_stream()
        self.refresh()

    def leave(self) -> None:
        self._active = False
        self._sync_stream()

    def _sync_stream(self) -> None:
        video = getattr(self.ctx, "video", None)
        if video is None:
            return
        want = self._current if self._active else None
        if want == self._streaming:
            return
        if self._streaming:
            video.release(self._streaming)
            self._streaming = None
        self.view.pixmap = None
        self.view.update()
        self.video_info.clear()
        if want and video.acquire(want):
            self._streaming = want

    def _show_video(self) -> None:
        video = getattr(self.ctx, "video", None)
        if video is None or not self._streaming:
            return
        sf = video.latest(self._streaming)
        st = video.stats(self._streaming)
        if sf is not None:
            self.view.pixmap = frame_pixmap(sf.image)
            self.view.frame_size = (sf.image.shape[1], sf.image.shape[0])
            self.view.update()
        if st is not None and not st.connected:
            self.video_info.setText(f"Потік: немає ({st.last_error or 'підключення…'})")
            self.video_info.setStyleSheet(f"color: {BAD}")
        elif sf is not None and st is not None:
            size = f"{st.frame_size[0]}×{st.frame_size[1]}" if st.frame_size else "…"
            ang = ("без кутів (немає звʼязку з сервоприводами)" if not sf.has_angles else
                   f"кадр знято при азимуті {(sf.angles.azimuth_deg + self.service.config(self._streaming).north_offset_deg) % 360:.1f}°, "
                   f"куті місця {sf.angles.elevation_deg:.1f}°")
            self.video_info.setText(f"{size}, {st.fps:.1f} кадрів/с · {ang}")
            self.video_info.setStyleSheet("" if sf.has_angles else f"color: {WARN}")

    def _on_select(self, item: Optional[QListWidgetItem], _prev=None) -> None:
        self._current = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._sync_stream()
        if self.calib is not None:
            self.calib.open_camera(self.service.config(self._current).camera_id if self._current else None)
            self._overlays_changed()
        self.title.setText(f"Обʼєкти управління / Управління · {self._current}" if self._current else "Обʼєкти управління / Управління")
        self.message.clear()
        self._load_form()
        self.refresh()

    def _load_demo(self) -> None:
        rec = self._record()
        self._loading_demo = True
        for box, value in ((self.demo_servos, rec.demo_servos if rec else False),
                           (self.demo_video, rec.demo_video if rec else False)):
            box.setChecked(bool(value))
            box.setEnabled(rec is not None)
        self._loading_demo = False

    def _record(self):
        if not self._current or self.ctx is None:
            return None
        cfg = self.service.config(self._current)
        return self.ctx.camera(cfg.camera_id)

    def _on_demo(self, servos=None, video=None) -> None:
        if getattr(self, "_loading_demo", False) or self.ctx is None or not self._current:
            return
        name = self._current
        self.ctx.set_demo(self.service.config(name).camera_id, servos=servos, video=video)
        what = "сервоприводів" if servos is not None else "відео"
        on = servos if servos is not None else video
        self._say(f"{name}: демо {what} {'увімкнено' if on else 'вимкнено'}")
        self._load_form()

    def _load_form(self) -> None:
        if not self._current:
            return
        self._load_demo()
        cfg = self.service.config(self._current)
        self.port.setText(cfg.port)
        idx = self.baud.findData(cfg.baudrate)
        self.baud.setCurrentIndex(idx if idx >= 0 else self.baud.count() - 1)
        for axis in AXES:
            self.cols[axis].load(getattr(cfg, axis))
        self.tgt_el.setRange(cfg.tilt.min_deg if cfg.tilt.min_deg is not None else 0.0,
                             cfg.tilt.max_deg if cfg.tilt.max_deg is not None else 90.0)

    # --- оновлення стану ----------------------------------------------------------
    def refresh(self) -> None:
        for st in self.service.statuses():
            item = self._items.get(st.name)
            if item is None:
                continue
            color = GOOD if st.link_ok and not st.error_flags else (WARN if st.link_ok else BAD)
            item.setIcon(_dot(color))
            demo = self.service.config(st.name).demo
            item.setText(f"{st.name} · {st.link_text}" + (" · демо" if demo else ""))
            item.setToolTip(st.error_text or st.link_text)
            if st.name == self._current:
                self._show_status(st)
        self._show_video()
        if self.calib is not None and self._active and self._current:
            self.calib.refresh(overlays=self.overlays.isChecked())

    def _show_status(self, st: CameraStatus) -> None:
        self.cur_az.setText(f"Азимут: {_fmt(st.azimuth_deg)}")
        self.cur_el.setText(f"Кут місця: {_fmt(st.elevation_deg)}")
        self.cur_move.setText("Рух: рухається" if st.moving else "Рух: стоїть")
        self.link_label.setText(st.link_text)
        self.link_label.setStyleSheet(f"color: {GOOD if st.link_ok else BAD}")
        err = st.error_text
        ros_error = getattr(self.service.backend, "ros_error", "")
        if not st.link_ok and ros_error and not self.service.config(st.name).demo:
            err = ros_error + " — увімкніть «Демо сервоприводів» або встановіть ROS 2"
        elif not st.link_ok and not self.service.config(st.name).demo:
            info = getattr(getattr(self.service.backend, "ros", None), "connection_info", None)
            if info is not None:                 # rosbridge: з яким Pi і чому немає звʼязку
                err = "; ".join(t for t in (err, info(st.name)) if t)
        self.error_label.setText(err)
        self.error_label.setVisible(bool(err))
        cfg = self.service.config(st.name)
        self.cols["pan"].show_telemetry(st.pan, cfg.pan.temperature_limit_c)
        self.cols["tilt"].show_telemetry(st.tilt, cfg.tilt.temperature_limit_c)
        learning = self.service.is_learning(st.name)
        for w in self._manual_widgets:
            w.setEnabled(st.link_ok and not learning)
        for w in (self.scan_btn, self.set_id_btn, self.zero_btn):
            w.setEnabled(st.link_ok)

    # --- дії --------------------------------------------------------------------------
    def _say_color(self, text: str, color: str = "") -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {color}" if color else "")

    def _say(self, text: str, error: bool = False, warn: bool = False) -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {BAD}" if error else (f"color: {WARN}; font-weight: 600" if warn else ""))

    def _watch(self, future: Future, fn: Callable[[Future], None]) -> None:
        future.add_done_callback(lambda f: self._bridge.done.emit(fn, f))

    def _on_go(self) -> None:
        if not self._current:
            return
        note = self.service.limit_note(self._current, self.tgt_az.value(), self.tgt_el.value())
        try:
            az, el = self.service.go_to(self._current, self.tgt_az.value(), self.tgt_el.value(),
                                        self.speed.value())
        except (ManualControlLocked, RuntimeError) as e:
            self._say(str(e), error=True)
            return
        if note:
            self._say(f"{note}. Ціль обмежено: азимут {az:.1f}°, кут місця {el:.1f}°", warn=True)
        else:
            self._say(f"Перехід до азимуту {az:.1f}°, кута місця {el:.1f}°")

    def _on_jog(self, d_az: int, d_el: int) -> None:
        if not self._current:
            return
        step = self.step.value()
        try:
            note = self.service.jog(self._current, d_az * step, d_el * step, self.speed.value())
        except (ManualControlLocked, RuntimeError) as e:
            self._say(str(e), error=True)
            return
        if note:
            self._say(note, warn=True)
        elif self.message.text().startswith("Досягнуто межі"):
            self._say("")

    def _on_stop(self) -> None:
        if self._current:
            self._watch(self.service.stop(self._current), self._report_stop)

    def _report_stop(self, f: Future) -> None:
        try:
            f.result()
            self._say("Сервоприводи зупинено")
        except Exception as e:  # noqa: BLE001
            self._say(f"Стоп не підтверджено: {e}", error=True)

    def form_config(self) -> CameraControlConfig:
        cfg = copy.deepcopy(self.service.config(self._current))
        cfg.port = self.port.text().strip()
        cfg.baudrate = self.baud.currentData()
        cfg.pan = self.cols["pan"].read()
        cfg.tilt = self.cols["tilt"].read()
        return cfg

    def _on_save(self) -> None:
        if not self._current:
            return
        try:
            future = self.service.save_config(self.form_config())
        except ValueError as e:
            self._say(f"Не збережено: {e}", error=True)
            return
        self._say("Збережено в базі; надсилаю на Raspberry Pi…")
        self._load_form()

        def report(f: Future):
            try:
                ok, msg = f.result()
                self._say(f"Збережено і застосовано на Pi: {msg}" if ok else f"Pi не прийняв налаштування: {msg}",
                          error=not ok)
            except Exception as e:  # noqa: BLE001
                self._say(f"Збережено в базі, але не надіслано на Pi: {e}. Надішлю, коли камера вийде на звʼязок.",
                          error=True)
        self._watch(future, report)

    def _on_scan(self) -> None:
        if not self._current:
            return
        self._say("Опитую шину…")
        self.scan_btn.setEnabled(False)

        def report(f: Future):
            self.scan_btn.setEnabled(True)
            try:
                ids = f.result()
                self._say("Знайдено сервоприводи з ID: " + (", ".join(map(str, ids)) if ids else "жодного"))
            except Exception as e:  # noqa: BLE001
                self._say(f"Опитування не вдалося: {e}", error=True)
        self._watch(self.service.scan(self._current), report)

    def _on_set_id(self) -> None:
        if not self._current:
            return
        old, ok = QInputDialog.getInt(self, "Змінити ID", "Поточний ID сервопривода:", 1, 1, 253)
        if not ok:
            return
        new, ok = QInputDialog.getInt(self, "Змінити ID", "Новий ID (на шині має бути лише цей сервопривід):",
                                      2, 1, 253)
        if ok:
            self.change_servo_id(old, new)

    def change_servo_id(self, old: int, new: int) -> None:
        def report(f: Future):
            try:
                ok, msg = f.result()
                self._say(f"ID змінено: {msg}. Вкажіть новий ID у налаштуваннях і збережіть." if ok
                          else f"ID не змінено: {msg}", error=not ok)
            except Exception as e:  # noqa: BLE001
                self._say(f"ID не змінено: {e}", error=True)
        self._watch(self.service.set_servo_id(self._current, old, new), report)

    def _on_zero(self, axes) -> None:
        if not self._current:
            return
        name = self._current
        for axis in axes:
            def report(f: Future, axis=axis):
                try:
                    value = f.result()
                    self._say(f"Нуль {AXIS_TITLE[axis].split()[0].lower()} осі: зміщення {value:.2f}°, збережено")
                    if name == self._current:
                        self.cols[axis].zero.setValue(value)
                except Exception as e:  # noqa: BLE001
                    self._say(f"Нуль не встановлено: {e}", error=True)
            self._watch(self.service.set_zero(name, axis), report)
