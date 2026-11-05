"""Підменю «Камери / Редагування»: одна форма для додавання і редагування камери."""
from __future__ import annotations

import copy
from typing import Dict, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from polar_core import cameras as cm
from polar_core.cameras import CODECS, PROTOCOLS, STREAM_KINDS, TRANSPORTS, CameraRecord

from .common import BAD, GOOD, QUIET, WARN, FutureBridge, frame_pixmap

# (група, поле, підпис, вид, параметри)
FIELDS = [
    ("Загальне", "name", "Назва *", "text", {}),
    ("Загальне", "model", "Модель", "text", {}),
    ("Оптика", "fov_x_deg", "Кут огляду X (HFOV), ° *", "float", dict(lo=0.1, hi=180, dec=2)),
    ("Оптика", "fov_y_deg", "Кут огляду Y (VFOV), ° *", "float", dict(lo=0.1, hi=180, dec=2)),
    ("Оптика", "k1", "Дисторсія k1", "float", dict(lo=-10, hi=10, dec=5)),
    ("Оптика", "k2", "Дисторсія k2", "float", dict(lo=-10, hi=10, dec=5)),
    ("Оптика", "p1", "Дисторсія p1", "float", dict(lo=-10, hi=10, dec=5)),
    ("Оптика", "p2", "Дисторсія p2", "float", dict(lo=-10, hi=10, dec=5)),
    ("Оптика", "k3", "Дисторсія k3", "float", dict(lo=-10, hi=10, dec=5)),
    ("Розміщення", "latitude", "Широта, ° *", "optfloat", dict(ph="напр. 50.4501")),
    ("Розміщення", "longitude", "Довгота, ° *", "optfloat", dict(ph="напр. 30.5234")),
    ("Розміщення", "altitude_m", "Висота над рівнем моря, м *", "optfloat", {}),
    ("Розміщення", "north_offset_deg", "Зміщення нуля датчика від півночі, ° *", "float",
     dict(lo=0, hi=359.99, dec=2)),
    ("Розміщення", "tilt_deg", "Нахил камери відносно вертикалі, ° *", "float", dict(lo=-180, hi=180, dec=2)),
    ("Відеопотік", "demo_video", "Демо відео", "check", dict(text="синтетична сцена замість потоку камери")),
    ("Відеопотік", "protocol", "Протокол *", "combo", dict(items=[(p, p) for p in PROTOCOLS])),
    ("Відеопотік", "address", "Адреса *", "text", dict(ph="rtsp://192.168.1.10:554/stream1")),
    ("Відеопотік", "login", "Логін", "text", {}),
    ("Відеопотік", "password", "Пароль", "password", {}),
    ("Відеопотік", "stream_kind", "Потік *", "combo", dict(items=list(STREAM_KINDS.items()))),
    ("Відеопотік", "codec", "Кодек *", "combo", dict(items=[(c, c) for c in CODECS])),
    ("Відеопотік", "stream_res_x", "Роздільна здатність X, px *", "optint",
     dict(ph="заповнює «Перевірити зʼєднання»")),
    ("Відеопотік", "stream_res_y", "Роздільна здатність Y, px *", "optint",
     dict(ph="заповнює «Перевірити зʼєднання»")),
    ("Відеопотік", "fps", "Кадрів/с *", "float", dict(lo=0.1, hi=240, dec=1)),
    ("Відеопотік", "max_detection_latency_ms", "Допустима затримка виявлення, мс", "int", dict(lo=1, hi=60000)),
    ("Відеопотік", "transport", "Транспорт *", "combo", dict(items=[(t, t) for t in TRANSPORTS])),
    ("Відеопотік", "buffer_ms", "Буфер, мс", "int", dict(lo=0, hi=10000)),
    ("Відеопотік", "video_latency_ms", "Затримка відео відносно датчика, мс", "int", dict(lo=0, hi=5000)),
    ("Управління", "demo_servos", "Демо сервоприводів", "check",
     dict(text="імітатор замість Raspberry Pi і ROS 2")),
    ("Управління", "ros_namespace", "Простір імен ROS 2 *", "text", dict(ph="camera_1")),
    ("Управління", "az_min", "Межі азимуту, ° (мін)", "optfloat", dict(ph="без меж")),
    ("Управління", "az_max", "Межі азимуту, ° (макс)", "optfloat", dict(ph="без меж")),
    ("Управління", "el_min", "Межі кута місця, ° (мін)", "float", dict(lo=0, hi=90, dec=1)),
    ("Управління", "el_max", "Межі кута місця, ° (макс)", "float", dict(lo=0, hi=90, dec=1)),
    ("Управління", "max_speed_deg_s", "Найбільша швидкість, °/с", "float", dict(lo=0.1, hi=360, dec=0)),
]

HINTS = {
    "address": "URL, IP:порт або номер USB-пристрою — залежно від протоколу",
    "stream_kind": "основний — для панорами, додатковий — для перегляду",
    "buffer_ms": "менший буфер — точніша відповідність кадру кутам",
    "video_latency_ms": "вимірюється кнопкою «Виміряти затримку»; кути для кадру беруться на цей час раніше",
    "max_detection_latency_ms": "від кадру до запису цілі в таблицю; якщо перевищена — попередження",
    "az_min": "порожньо — без меж (ковзний контакт)",
    "demo_video": "для перевірки без камери: кадри малюються за кутами камери; адреса потоку не потрібна",
    "demo_servos": "для перевірки без Raspberry Pi: сервоприводи імітуються на сервері",
}


def _parse_opt(text: str, kind: str):
    t = text.strip().replace(",", ".")
    if not t:
        return None
    try:
        return int(t) if kind == "optint" else float(t)
    except ValueError:
        return "error"


class CameraForm(QWidget):
    closed = pyqtSignal()              # «Скасувати» або після збереження — назад до списку
    saved = pyqtSignal(int)

    def __init__(self, app_ctx, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = app_ctx
        self.rec = CameraRecord()
        self.bridge = FutureBridge()
        self.widgets: Dict[str, QWidget] = {}
        self.errors: Dict[str, QLabel] = {}
        self._loading = False

        outer = QHBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        form_host = QWidget()
        self.form_layout = QVBoxLayout(form_host)
        self.title = QLabel("Камери / Редагування")
        self.title.setStyleSheet("font-size: 15px; font-weight: 600")
        self.form_layout.addWidget(self.title)
        self._build_fields()
        self.form_layout.addLayout(self._build_buttons())
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.form_layout.addWidget(self.message)
        self.form_layout.addStretch(1)
        scroll.setWidget(form_host)
        outer.addWidget(scroll, 3)
        outer.addWidget(self._build_video_panel(), 2)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_video)
        self.timer.start(200)

    # --- побудова ----------------------------------------------------------------------
    def _build_fields(self) -> None:
        groups: Dict[str, QFormLayout] = {}
        for group, attr, label, kind, opt in FIELDS:
            if group not in groups:
                box = QGroupBox(group)
                groups[group] = QFormLayout(box)
                groups[group].setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                self.form_layout.addWidget(box)
            w = self._make_widget(kind, opt)
            if attr in HINTS:
                w.setToolTip(HINTS[attr])
            self.widgets[attr] = w
            err = QLabel("")
            err.setStyleSheet(f"color: {BAD}; font-size: 11px")
            err.setVisible(False)
            self.errors[attr] = err
            cell = QWidget()
            lay = QVBoxLayout(cell)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(1)
            if attr == "address":
                row = QHBoxLayout()
                row.addWidget(w, 1)
                self.usb_btn = QPushButton("Отримати список камер")
                self.usb_btn.setToolTip("USB-камери, підключені до сервера; вибір заповнює адресу, "
                                        "роздільну здатність потоку і кадри/с")
                self.usb_btn.clicked.connect(self.list_usb_cameras)
                row.addWidget(self.usb_btn)
                lay.addLayout(row)
            else:
                lay.addWidget(w)
            lay.addWidget(err)
            groups[group].addRow(label, cell)
            self._connect_change(w)
        self.widgets["protocol"].currentIndexChanged.connect(self._protocol_hint)

    def _make_widget(self, kind: str, opt: dict) -> QWidget:
        if kind in ("text", "password", "optfloat", "optint"):
            w = QLineEdit()
            if kind == "password":
                w.setEchoMode(QLineEdit.EchoMode.Password)
            if opt.get("ph"):
                w.setPlaceholderText(opt["ph"])
            return w
        if kind == "int":
            w = QSpinBox()
            w.setRange(opt["lo"], opt["hi"])
            return w
        if kind == "float":
            w = QDoubleSpinBox()
            w.setRange(opt["lo"], opt["hi"])
            w.setDecimals(opt.get("dec", 2))
            return w
        if kind == "check":
            return QCheckBox(opt.get("text", ""))
        if kind == "combo":
            w = QComboBox()
            for value, text in opt["items"]:
                w.addItem(text, value)
            return w
        raise ValueError(kind)

    def _connect_change(self, w: QWidget) -> None:
        sig = {QLineEdit: "textChanged", QSpinBox: "valueChanged", QDoubleSpinBox: "valueChanged",
               QComboBox: "currentIndexChanged", QCheckBox: "toggled"}[type(w)]
        getattr(w, sig).connect(lambda *_: self.validate())

    def _build_buttons(self) -> QHBoxLayout:
        h = QHBoxLayout()
        self.check_btn = QPushButton("Перевірити зʼєднання")
        self.check_btn.clicked.connect(self.check_connection)
        self.latency_btn = QPushButton("Виміряти затримку")
        self.latency_btn.setToolTip("Камера повернеться на ±25° і назад; затримка відео запишеться в параметри")
        self.latency_btn.clicked.connect(self.measure_latency)
        self.save_btn = QPushButton("Зберегти")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.save)
        self.cancel_btn = QPushButton("Скасувати")
        self.cancel_btn.clicked.connect(self.closed.emit)
        h.addWidget(self.check_btn)
        h.addWidget(self.latency_btn)
        h.addStretch(1)
        h.addWidget(self.cancel_btn)
        h.addWidget(self.save_btn)
        return h

    def _build_video_panel(self) -> QWidget:
        box = QGroupBox("Відео")
        v = QVBoxLayout(box)
        self.preview = QLabel("Немає кадру")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(320, 180)
        self.preview.setStyleSheet("background: #202020; color: #bbbbbb")
        v.addWidget(self.preview)
        self.frame_info = QLabel("")
        self.frame_info.setWordWrap(True)
        v.addWidget(self.frame_info)
        self.stream_info = QLabel("")
        self.stream_info.setWordWrap(True)
        v.addWidget(self.stream_info)
        v.addStretch(1)
        self._check_pixmap = None
        return box

    # --- дані форми --------------------------------------------------------------------
    def open_camera(self, camera_id: Optional[int]) -> None:
        self.rec = self.ctx.camera(camera_id) if camera_id is not None else CameraRecord(
            ros_namespace=f"camera_{len(self.ctx.records()) + 1}")
        self.title.setText(f"Камери / Редагування · {self.rec.name}" if camera_id else "Камери / Додати камеру")
        self._check_pixmap = None
        self.preview.clear()
        self.preview.setText("Немає кадру")
        self.frame_info.clear()
        self.stream_info.clear()
        self.message.clear()
        self._loading = True
        for _g, attr, _l, kind, _o in FIELDS:
            w = self.widgets[attr]
            value = getattr(self.rec, attr)
            if kind == "password":
                w.clear()
                w.setPlaceholderText("збережено — залиште порожнім, щоб не змінювати" if self.rec.has_password else "")
            elif isinstance(w, QLineEdit):
                w.setText("" if value is None else (f"{value:g}" if isinstance(value, float) else str(value)))
            elif isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, w.findData(value)))
            elif isinstance(w, QCheckBox):
                w.setChecked(bool(value))
            else:
                w.setValue(value)
        self._loading = False
        self.latency_btn.setEnabled(camera_id is not None)
        self._protocol_hint()
        self.validate()

    def form_record(self) -> CameraRecord:
        rec = copy.copy(self.rec)
        self._parse_errors: Dict[str, str] = {}
        for _g, attr, _l, kind, _o in FIELDS:
            w = self.widgets[attr]
            if kind == "password":
                rec.password = w.text() or None
            elif kind in ("optfloat", "optint"):
                v = _parse_opt(w.text(), kind)
                if v == "error":
                    self._parse_errors[attr] = "введіть число"
                    v = None
                setattr(rec, attr, v)
            elif kind == "text":
                setattr(rec, attr, w.text().strip())
            elif isinstance(w, QComboBox):
                setattr(rec, attr, w.currentData())
            elif isinstance(w, QCheckBox):
                setattr(rec, attr, w.isChecked())
            else:
                setattr(rec, attr, w.value())
        return rec

    def validate(self) -> Dict[str, str]:
        if self._loading:
            return {}
        rec = self.form_record()
        errs = rec.validate(self.ctx.records())
        errs.update(self._parse_errors)
        for attr, lab in self.errors.items():
            lab.setText(errs.get(attr, ""))
            lab.setVisible(attr in errs)
        self.save_btn.setEnabled(not errs)
        self.check_btn.setEnabled("address" not in errs and "protocol" not in errs)
        self.latency_btn.setEnabled(self.rec.id is not None)
        return errs

    def _protocol_hint(self) -> None:
        self.usb_btn.setEnabled(self.widgets["protocol"].currentData() == "USB")
        ph = {"RTSP": "rtsp://192.168.1.10:554/stream1", "ONVIF": "192.168.1.10:80",
              "USB": "0 або /dev/video0", "HTTP": "http://192.168.1.10/video.mjpg"}
        self.widgets["address"].setPlaceholderText(ph.get(self.widgets["protocol"].currentData(), ""))

    # --- дії -------------------------------------------------------------------------------
    def _say(self, text: str, color: str = "") -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {color}" if color else "")

    def save(self) -> bool:
        if self.validate():
            return False
        rec = self.form_record()
        try:
            cid, stale = self.ctx.save_camera(rec)
        except ValueError as e:
            self._say(f"Не збережено: {e}", BAD)
            return False
        self.saved.emit(cid)
        self.open_camera(cid)
        if stale:
            self._say("Збережено. Змінилась роздільна здатність або кут огляду — панораму камери позначено "
                      "застарілою, потрібен новий огляд (режим Навчання).", WARN)
        else:
            self._say("Збережено.", GOOD)
        return True

    def check_connection(self) -> None:
        rec = self.form_record()
        if rec.password is None and rec.id and rec.has_password:
            with self.ctx.control._db_lock:
                rec.password = cm.stream_password(self.ctx.conn, self.ctx.box, rec.id)
        self._say("Підключаюся до потоку…")
        self.check_btn.setEnabled(False)

        def report(f):
            self.check_btn.setEnabled(True)
            try:
                img, (w, h), fps = f.result()
            except Exception as e:  # noqa: BLE001
                self._say(f"Зʼєднання не вдалося: {e}", BAD)
                return
            self._check_pixmap = frame_pixmap(img)
            notes = []
            if (rec.stream_res_x, rec.stream_res_y) != (w, h):
                if rec.stream_res_x is not None:
                    notes.append(f"роздільну здатність змінено з {rec.stream_res_x}×{rec.stream_res_y}")
                self.widgets["stream_res_x"].setText(str(w))      # фактична роздільна здатність потоку
                self.widgets["stream_res_y"].setText(str(h))
            if fps and abs(fps - rec.fps) > max(1.0, rec.fps * 0.2):
                notes.append(f"у формі {rec.fps:g} кадрів/с")
            text = f"Зʼєднання працює: {w}×{h}, {fps:.1f} кадрів/с"
            self._say(text + (f" ({'; '.join(notes)})" if notes else ""), WARN if notes else GOOD)
        self.bridge.watch(self.ctx.video.check_connection(rec), report)

    def list_usb_cameras(self) -> None:
        self._say("Шукаю USB-камери…")
        self.usb_btn.setEnabled(False)

        def report(f):
            self._protocol_hint()
            try:
                cams = f.result()
            except Exception as e:  # noqa: BLE001
                self._say(f"Список камер не отримано: {e}", BAD)
                return
            self._say(f"Знайдено USB-камер: {len(cams)}." if cams else "До сервера не підключено жодної USB-камери.",
                      "" if cams else WARN)
            self.open_usb_dialog(cams)
        self.bridge.watch(self.ctx.video.list_usb_cameras(), report)

    def usb_in_use(self) -> Dict[str, str]:
        """Адреси USB-камер, вже доданих у Polar (крім цієї) → назва камери."""
        return {r.address.strip(): r.name for r in self.ctx.records()
                if r.protocol == "USB" and r.id != self.rec.id}

    def open_usb_dialog(self, cams) -> None:
        from .usb_dialog import UsbCameraDialog

        self.usb_dialog = UsbCameraDialog(cams, self.usb_in_use(), self)
        self.usb_dialog.accepted.connect(
            lambda: self.apply_usb_choice(self.usb_dialog.selected_camera(), self.usb_dialog.selected_mode()))
        self.usb_dialog.open()

    def apply_usb_choice(self, cam, mode) -> None:
        if cam is None:
            return
        self.widgets["protocol"].setCurrentIndex(self.widgets["protocol"].findData("USB"))
        self.widgets["address"].setText(cam.address)
        if not self.widgets["model"].text().strip():
            self.widgets["model"].setText(cam.name)
        if mode is not None:
            self.widgets["stream_res_x"].setText(str(mode.width))
            self.widgets["stream_res_y"].setText(str(mode.height))
            if mode.fps:
                self.widgets["fps"].setValue(mode.fps)
            if mode.codec:
                self.widgets["codec"].setCurrentIndex(self.widgets["codec"].findData(mode.codec))
        used = self.usb_in_use().get(cam.address) or self.usb_in_use().get(cam.device)
        self._say(f"Вибрано {cam.name} ({cam.address})" + (f" — вже використовується камерою «{used}»" if used else ""),
                  WARN if used else GOOD)
        self.validate()

    def measure_latency(self) -> None:
        if self.rec.id is None:
            return
        self._say("Вимірюю затримку: камера повертається…")
        self.latency_btn.setEnabled(False)
        try:
            fut = self.ctx.video.measure_latency(self.rec.name)
        except Exception as e:  # noqa: BLE001
            self._say(str(e), BAD)
            self.latency_btn.setEnabled(True)
            return

        def report(f):
            self.latency_btn.setEnabled(True)
            try:
                ms, q = f.result()
            except Exception as e:  # noqa: BLE001
                self._say(f"Затримку не виміряно: {e}", BAD)
                return
            self.widgets["video_latency_ms"].setValue(ms)
            self.rec.video_latency_ms = ms
            self._say(f"Затримка відео відносно датчика: {ms} мс (збіг {q:.2f}); збережено.", GOOD)
        self.bridge.watch(fut, report)

    # --- живе відео ------------------------------------------------------------------------
    def _refresh_video(self) -> None:
        if not self.isVisible():
            return
        name = self.rec.name if self.rec.id else None
        sf = self.ctx.video.latest(name) if name else None
        pix = frame_pixmap(sf.image) if sf is not None else self._check_pixmap
        if pix is not None:
            self.preview.setPixmap(pix.scaled(self.preview.width(), self.preview.height(),
                                              Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))
        if sf is not None:
            if sf.has_angles:
                a = sf.angles
                north = (a.azimuth_deg + self.rec.north_offset_deg) % 360.0
                self.frame_info.setText(
                    f"Кадр №{sf.index}: азимут {north:.2f}°, кут місця {a.elevation_deg:.2f}°"
                    + (" · рухається" if a.moving else "") + f" · кути за {a.gap_s * 1000:.0f} мс від зчитування")
                self.frame_info.setStyleSheet("")
            else:
                self.frame_info.setText(f"Кадр №{sf.index}: немає кутів (немає звʼязку з камерою)")
                self.frame_info.setStyleSheet(f"color: {BAD}")
        st = self.ctx.video.stats(name) if name else None
        if st is None:
            self.stream_info.setText("Потік запуститься після збереження камери; "
                                     "кадр можна побачити кнопкою «Перевірити зʼєднання»" if not name else "")
            self.stream_info.setStyleSheet(f"color: {QUIET}")
            return
        if not st.connected:
            self.stream_info.setText(f"Потік: немає ({st.last_error or 'підключення…'}), спроб: {st.reconnects}")
            self.stream_info.setStyleSheet(f"color: {BAD}")
            return
        size = f"{st.frame_size[0]}×{st.frame_size[1]}" if st.frame_size else "…"
        lat = "…" if st.latency_p95_ms is None else f"{st.latency_p95_ms:.0f} мс"
        self.stream_info.setText(
            f"Потік: {size}, {st.fps:.1f} кадрів/с · затримка (95 %) {lat} з допустимих "
            f"{st.latency_limit_ms:.0f} мс · кадрів: {st.frames}, без кутів: {st.frames_without_angles}")
        self.stream_info.setStyleSheet(f"color: {GOOD if st.latency_ok else WARN}" if st.frames else f"color: {QUIET}")
