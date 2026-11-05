"""Камери: запис форми «Камери / Редагування», перевірка полів, читання й запис у базу."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional, Tuple

from .control import default_servo
from .secrets import SecretBox

PROTOCOLS = ("RTSP", "ONVIF", "USB", "HTTP")
STREAM_KINDS = {"main": "основний", "sub": "додатковий"}
CODECS = ("H.264", "H.265", "MJPEG")
TRANSPORTS = ("TCP", "UDP")


@dataclass
class CameraRecord:
    # Загальне
    name: str = ""
    model: str = ""
    # Оптика
    resolution_x: int = 1920
    resolution_y: int = 1080
    fov_x_deg: float = 60.0
    fov_y_deg: float = 34.0
    k1: float = 0.0
    k2: float = 0.0
    p1: float = 0.0
    p2: float = 0.0
    k3: float = 0.0
    # Розміщення
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude_m: Optional[float] = None
    north_offset_deg: float = 0.0
    tilt_deg: float = 0.0
    # Відеопотік
    protocol: str = "RTSP"
    address: str = ""
    login: str = ""
    password: Optional[str] = None          # None — не змінювати збережений
    stream_kind: str = "main"
    codec: str = "H.264"
    stream_res_x: Optional[int] = None      # None — як у камери
    stream_res_y: Optional[int] = None
    fps: float = 25.0
    max_detection_latency_ms: int = 500
    transport: str = "TCP"
    buffer_ms: int = 0
    video_latency_ms: int = 0               # виміряна затримка відео відносно енкодерів
    # Управління
    ros_namespace: str = ""
    az_min: Optional[float] = None          # None — без меж (ковзний контакт)
    az_max: Optional[float] = None
    el_min: float = 0.0
    el_max: float = 90.0
    max_speed_deg_s: float = 180.0
    # Демо: імітатор замість справжнього пристрою
    demo_servos: bool = False               # сервоприводи й Raspberry Pi — імітатор, без ROS 2
    demo_video: bool = False                # синтетична сцена замість відеопотоку
    # службове
    id: Optional[int] = None
    has_password: bool = False
    panorama_path: str = ""
    panorama_stale: bool = False

    @property
    def frame_size(self) -> Tuple[int, int]:
        """Розмір кадрів, що реально приходять (потік або сама камера)."""
        return (self.stream_res_x or self.resolution_x, self.stream_res_y or self.resolution_y)

    @property
    def distortion(self) -> Tuple[float, float, float, float, float]:
        return (self.k1, self.k2, self.p1, self.p2, self.k3)

    def validate(self, others: List["CameraRecord"] = ()) -> Dict[str, str]:
        """Помилки за назвою поля — форма показує їх біля полів."""
        e: Dict[str, str] = {}
        if not self.name.strip():
            e["name"] = "обовʼязкове поле"
        elif any(o.name.strip().lower() == self.name.strip().lower() and o.id != self.id for o in others):
            e["name"] = "камера з такою назвою вже є"
        for f in ("resolution_x", "resolution_y"):
            if not isinstance(getattr(self, f), int) or getattr(self, f) <= 0:
                e[f] = "ціле число більше 0"
        for f in ("fov_x_deg", "fov_y_deg"):
            if not 0 < getattr(self, f) <= 180:
                e[f] = "0–180°"
        if self.latitude is None or not -90 <= self.latitude <= 90:
            e["latitude"] = "−90…90"
        if self.longitude is None or not -180 <= self.longitude <= 180:
            e["longitude"] = "−180…180"
        if self.altitude_m is None:
            e["altitude_m"] = "обовʼязкове поле"
        if not 0 <= self.north_offset_deg < 360:
            e["north_offset_deg"] = "0–360°"
        if not -180 <= self.tilt_deg <= 180:
            e["tilt_deg"] = "−180…180°"
        if self.protocol not in PROTOCOLS:
            e["protocol"] = "оберіть зі списку"
        addr_error = None if self.demo_video and not self.address.strip() else address_error(
            self.protocol, self.address)       # у демо відео адреса не потрібна
        if addr_error:
            e["address"] = addr_error
        elif self.protocol == "USB" and not self.demo_video:
            twin = next((o for o in others if o.protocol == "USB" and o.id != self.id and not o.demo_video
                         and o.address.strip() == self.address.strip()), None)
            if twin is not None:
                e["address"] = f"цю USB-камеру вже використовує «{twin.name}»"
        if self.stream_kind not in STREAM_KINDS:
            e["stream_kind"] = "оберіть зі списку"
        if self.codec not in CODECS:
            e["codec"] = "оберіть зі списку"
        for f in ("stream_res_x", "stream_res_y"):
            v = getattr(self, f)
            if v is not None and v <= 0:
                e[f] = "більше 0 або порожньо"
        if not self.fps > 0:
            e["fps"] = "більше 0"
        if self.max_detection_latency_ms <= 0:
            e["max_detection_latency_ms"] = "більше 0"
        if self.transport not in TRANSPORTS:
            e["transport"] = "оберіть зі списку"
        if self.buffer_ms < 0:
            e["buffer_ms"] = "0 або більше"
        if not re.fullmatch(r"/?[A-Za-z][A-Za-z0-9_]*(/[A-Za-z][A-Za-z0-9_]*)*", self.ros_namespace or ""):
            e["ros_namespace"] = "латиниця, цифри, _; наприклад camera_1"
        elif any(o.ros_namespace.strip("/") == self.ros_namespace.strip("/") and o.id != self.id for o in others):
            e["ros_namespace"] = "вже використовується іншою камерою"
        if (self.az_min is None) != (self.az_max is None):
            e["az_min"] = "обидві межі або жодної"
        elif self.az_min is not None and not 0 <= self.az_min < self.az_max <= 360:
            e["az_min"] = "0 ≤ мін < макс ≤ 360"
        if not 0 <= self.el_min < self.el_max <= 90:
            e["el_min"] = "0 ≤ мін < макс ≤ 90"
        if not 0 < self.max_speed_deg_s <= 360:
            e["max_speed_deg_s"] = "0–360 °/с"
        return e


def address_error(protocol: str, address: str) -> Optional[str]:
    a = address.strip()
    if not a:
        return "обовʼязкове поле"
    if protocol == "RTSP" and not re.match(r"rtsps?://[^/\s]+", a, re.I):
        return "rtsp://адреса[:порт]/шлях"
    if protocol == "HTTP" and not re.match(r"https?://[^/\s]+", a, re.I):
        return "http://адреса[:порт]/шлях"
    if protocol == "USB" and not re.fullmatch(r"\d+|/dev/video\d+|/dev/v4l/by-(id|path)/\S+", a):
        return "номер пристрою (0, 1 …), /dev/videoN або /dev/v4l/by-id/…"
    if protocol == "ONVIF" and not re.fullmatch(r"(https?://)?[\w.\-]+(:\d+)?/?", a, re.I):
        return "IP:порт пристрою ONVIF"
    if "@" in a.split("//")[-1].split("/")[0]:
        return "логін і пароль — в окремих полях, не в адресі"
    return None


# --- база даних ----------------------------------------------------------------
_COLS = {
    # поле запису: стовпець таблиці
    "name": "name", "model": "model", "resolution_x": "resolution_x", "resolution_y": "resolution_y",
    "fov_x_deg": "fov_x_deg", "fov_y_deg": "fov_y_deg", "k1": "k1", "k2": "k2", "p1": "p1", "p2": "p2",
    "k3": "k3", "latitude": "latitude", "longitude": "longitude", "altitude_m": "altitude_m",
    "north_offset_deg": "north_offset_deg", "tilt_deg": "tilt_deg", "protocol": "stream_protocol",
    "address": "stream_url", "login": "stream_login", "stream_kind": "stream_kind", "codec": "stream_codec",
    "stream_res_x": "stream_res_x", "stream_res_y": "stream_res_y", "fps": "stream_fps",
    "max_detection_latency_ms": "max_detection_latency_ms", "transport": "stream_transport",
    "buffer_ms": "stream_buffer_ms", "video_latency_ms": "stream_latency_ms",
    "ros_namespace": "ros_namespace", "panorama_path": "panorama_path",
    "demo_servos": "demo_servos", "demo_video": "demo_video",
}


def _servo_row(conn, camera_id, axis):
    return conn.execute("SELECT min_deg, max_deg, max_speed_deg_s FROM servo_settings "
                        "WHERE camera_id = ? AND axis = ?", (camera_id, axis)).fetchone()


def list_cameras(conn: sqlite3.Connection) -> List[CameraRecord]:
    ids = [r[0] for r in conn.execute("SELECT id FROM cameras ORDER BY id")]
    return [get_camera(conn, i) for i in ids]


def get_camera(conn: sqlite3.Connection, camera_id: int) -> CameraRecord:
    cols = ", ".join(_COLS.values())
    row = conn.execute(f"SELECT {cols}, stream_password_enc, panorama_stale FROM cameras WHERE id = ?",
                       (camera_id,)).fetchone()
    if row is None:
        raise KeyError(camera_id)
    rec = CameraRecord(id=camera_id)
    for i, attr in enumerate(_COLS):
        setattr(rec, attr, row[i])
    rec.demo_servos, rec.demo_video = bool(rec.demo_servos), bool(rec.demo_video)
    rec.has_password = bool(row[len(_COLS)])
    rec.panorama_stale = bool(row[len(_COLS) + 1])
    pan, tilt = _servo_row(conn, camera_id, "pan"), _servo_row(conn, camera_id, "tilt")
    if pan:
        rec.az_min, rec.az_max, rec.max_speed_deg_s = pan
    if tilt:
        rec.el_min, rec.el_max = tilt[0], tilt[1]
        rec.max_speed_deg_s = min(rec.max_speed_deg_s, tilt[2]) if pan else tilt[2]
    if not rec.ros_namespace:
        rec.ros_namespace = f"camera_{camera_id}"
    return rec


def stream_password(conn: sqlite3.Connection, box: SecretBox, camera_id: int) -> str:
    token = conn.execute("SELECT stream_password_enc FROM cameras WHERE id = ?", (camera_id,)).fetchone()[0]
    return box.decrypt(token)


def save_camera(conn: sqlite3.Connection, rec: CameraRecord, box: SecretBox) -> Tuple[int, bool]:
    """Зберегти камеру. Повертає (id, панорама застаріла через зміну оптики)."""
    errors = rec.validate(list_cameras(conn))
    if errors:
        raise ValueError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    rec.ros_namespace = rec.ros_namespace.strip("/")
    values = {col: getattr(rec, attr) for attr, col in _COLS.items()}
    stale = False
    with conn:
        if rec.id is None:
            cols = ", ".join(values)
            cur = conn.execute(f"INSERT INTO cameras ({cols}) VALUES ({', '.join('?' * len(values))})",
                               list(values.values()))
            rec.id = cur.lastrowid
        else:
            old = get_camera(conn, rec.id)
            stale = (old.frame_size != rec.frame_size or old.resolution_x != rec.resolution_x
                     or old.resolution_y != rec.resolution_y or old.fov_x_deg != rec.fov_x_deg
                     or old.fov_y_deg != rec.fov_y_deg)
            sets = ", ".join(f"{c} = ?" for c in values)
            conn.execute(f"UPDATE cameras SET {sets}, updated_at = datetime('now')"
                         + (", panorama_stale = 1" if stale else "") + " WHERE id = ?",
                         [*values.values(), rec.id])
        if rec.password is not None:
            conn.execute("UPDATE cameras SET stream_password_enc = ? WHERE id = ?",
                         (box.encrypt(rec.password), rec.id))
        # група «Управління» живе в налаштуваннях сервоприводів
        for axis, lo, hi in (("pan", rec.az_min, rec.az_max), ("tilt", rec.el_min, rec.el_max)):
            if _servo_row(conn, rec.id, axis) is None:
                d = default_servo(axis)
                conn.execute("INSERT INTO servo_settings (camera_id, axis, servo_id) VALUES (?, ?, ?)",
                             (rec.id, axis, d.servo_id))
            conn.execute("UPDATE servo_settings SET min_deg = ?, max_deg = ?, max_speed_deg_s = ? "
                         "WHERE camera_id = ? AND axis = ?", (lo, hi, rec.max_speed_deg_s, rec.id, axis))
    rec.panorama_stale = rec.panorama_stale or stale
    rec.password = None
    rec.has_password = rec.has_password or bool(
        conn.execute("SELECT stream_password_enc FROM cameras WHERE id = ?", (rec.id,)).fetchone()[0])
    return rec.id, stale


def delete_camera(conn: sqlite3.Connection, camera_id: int) -> str:
    """Видалити камеру з налаштуваннями й подіями. Повертає шлях до її панорами (файл видаляє викликач)."""
    with conn:
        row = conn.execute("SELECT panorama_path FROM cameras WHERE id = ?", (camera_id,)).fetchone()
        if row is None:
            raise KeyError(camera_id)
        conn.execute("DELETE FROM events WHERE camera_id = ?", (camera_id,))
        conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
    return row[0]


def record_fields() -> List[str]:
    return [f.name for f in fields(CameraRecord)]


def set_demo(conn: sqlite3.Connection, camera_id: int, servos: Optional[bool] = None,
             video: Optional[bool] = None) -> None:
    """Увімкнути або вимкнути демо для однієї камери (з екрана «Управління»)."""
    with conn:
        if servos is not None:
            conn.execute("UPDATE cameras SET demo_servos = ? WHERE id = ?", (int(servos), camera_id))
        if video is not None:
            conn.execute("UPDATE cameras SET demo_video = ? WHERE id = ?", (int(video), camera_id))
