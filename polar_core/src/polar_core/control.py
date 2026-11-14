"""Керування камерами на боці сервера (polar_control) без залежності від ROS і інтерфейсу.

* налаштування сервоприводів і ROS 2 — читання/запис у базу;
* перерахунок азимуту від півночі в кут камери і назад, обмеження межами;
* стан звʼязку з Raspberry Pi за часом останнього повідомлення стану;
* блокування ручного керування під час Навчання.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional, Tuple

from .angles import azimuth_to_sensor, sensor_to_azimuth, wrap360

ERROR_SERVO_OFFLINE = 1
ERROR_OVERHEAT = 2
ERROR_LINK_TIMEOUT = 4
ERROR_STOPPED = 8

ERROR_TEXT = {
    ERROR_SERVO_OFFLINE: "сервопривід не відповідає",
    ERROR_OVERHEAT: "перегрів сервопривода",
    ERROR_LINK_TIMEOUT: "Pi втратив звʼязок із сервером",
    ERROR_STOPPED: "зупинено",
}

AXES = ("pan", "tilt")
BAUDRATES = (38_400, 57_600, 76_800, 115_200, 128_000, 250_000, 500_000, 1_000_000)
ST3020_MAX_SPEED_DEG_S = 360.0
RMW_CHOICES = {"Fast DDS": "rmw_fastrtps_cpp", "Cyclone DDS": "rmw_cyclonedds_cpp"}
# звʼязок сервера з Pi: rosbridge (WebSocket, ROS 2 на сервері не потрібен) або ROS 2 DDS напряму
TRANSPORTS = {"rosbridge (WebSocket) — без ROS 2 на сервері": "rosbridge", "ROS 2 DDS (rclpy)": "dds"}


def describe_errors(flags: int) -> str:
    return ", ".join(text for bit, text in ERROR_TEXT.items() if flags & bit)


class ManualControlLocked(RuntimeError):
    """Під час Навчання ручне керування заблоковане (крім «Стоп»)."""


# --- налаштування ------------------------------------------------------------
@dataclass
class ServoSettings:
    axis: str                      # pan | tilt
    servo_id: int
    direction: int = 1
    zero_offset_deg: float = 0.0
    gear_ratio: float = 1.0
    max_speed_deg_s: float = 180.0
    acceleration: int = 50
    torque_limit_pct: float = 100.0
    temperature_limit_c: float = 70.0
    min_deg: Optional[float] = None
    max_deg: Optional[float] = None

    @property
    def continuous(self) -> bool:
        """Азимут без меж — обертання через ковзний контакт."""
        return self.axis == "pan" and self.min_deg is None and self.max_deg is None

    def validate(self) -> List[str]:
        errors = []
        if self.axis not in AXES:
            errors.append(f"невідома вісь {self.axis}")
        if not 1 <= self.servo_id <= 253:
            errors.append("ID на шині має бути 1–253")
        if self.direction not in (1, -1):
            errors.append("напрямок: прямий або зворотний")
        if self.gear_ratio <= 0:
            errors.append("передавальне число має бути більше 0")
        if not 0 < self.max_speed_deg_s <= ST3020_MAX_SPEED_DEG_S * self.gear_ratio:
            errors.append(f"найбільша швидкість — до {ST3020_MAX_SPEED_DEG_S * self.gear_ratio:g} °/с")
        if not 0 <= self.acceleration <= 254:
            errors.append("прискорення — 0–254")
        if not 0 <= self.torque_limit_pct <= 100:
            errors.append("обмеження моменту — 0–100 %")
        if not 20 <= self.temperature_limit_c <= 85:
            errors.append("межа температури — 20–85 °C")
        if (self.min_deg is None) != (self.max_deg is None):
            errors.append("задайте обидві межі кута або жодної")
        elif self.min_deg is not None and self.min_deg >= self.max_deg:
            errors.append("нижня межа кута має бути меншою за верхню")
        if self.axis == "tilt" and self.min_deg is None:
            errors.append("для кута місця межі обовʼязкові")
        return errors

    def clamp(self, axis_deg: float) -> float:
        if self.min_deg is not None:
            axis_deg = max(axis_deg, self.min_deg)
        if self.max_deg is not None:
            axis_deg = min(axis_deg, self.max_deg)
        return axis_deg


def default_servo(axis: str) -> ServoSettings:
    if axis == "pan":
        return ServoSettings("pan", 1)
    return ServoSettings("tilt", 2, min_deg=0.0, max_deg=90.0)


@dataclass
class CameraControlConfig:
    camera_id: int
    name: str
    namespace: str                 # простір імен ROS 2, напр. camera_1
    north_offset_deg: float = 0.0
    port: str = "/dev/serial0"
    baudrate: int = 1_000_000
    pan: ServoSettings = field(default_factory=lambda: default_servo("pan"))
    tilt: ServoSettings = field(default_factory=lambda: default_servo("tilt"))
    demo: bool = False             # сервоприводи — імітатор (без ROS 2 і Raspberry Pi)
    host: str = ""                 # адреса Raspberry Pi для rosbridge (з форми камери або з адреси потоку)

    def validate(self) -> List[str]:
        errors = self.pan.validate() + self.tilt.validate()
        if self.pan.servo_id == self.tilt.servo_id:
            errors.append("ID горизонтального і вертикального сервоприводів мають різнитися")
        if self.baudrate not in BAUDRATES:
            errors.append("непідтримувана швидкість обміну")
        if not self.port.strip():
            errors.append("вкажіть порт")
        return errors


@dataclass
class RosSettings:
    transport: str = "rosbridge"     # rosbridge | dds
    bridge_port: int = 9090          # порт rosbridge_server на Pi
    domain_id: int = 0
    rmw: str = "rmw_fastrtps_cpp"
    interface: str = ""              # порожньо — усі інтерфейси
    discovery_server: str = ""       # порожньо — multicast; інакше адреса:порт
    topic_prefix: str = "/polar"
    state_rate_hz: float = 20.0
    link_timeout_s: float = 2.0
    sros_enabled: bool = False
    sros_keystore: str = ""

    def validate(self) -> List[str]:
        errors = []
        if self.transport not in TRANSPORTS.values():
            errors.append("невідомий спосіб звʼязку з Pi")
        if not 1 <= self.bridge_port <= 65535:
            errors.append("порт rosbridge — 1–65535")
        if not 0 <= self.domain_id <= 101:
            errors.append("ROS_DOMAIN_ID — 0–101")
        if self.rmw not in RMW_CHOICES.values():
            errors.append("невідома реалізація DDS")
        if not self.topic_prefix.startswith("/"):
            errors.append("префікс топіків має починатися з /")
        if not 0.5 <= self.state_rate_hz <= 100:
            errors.append("частота стану — 0,5–100 Гц")
        if not 0.2 <= self.link_timeout_s <= 60:
            errors.append("тайм-аут звʼязку — 0,2–60 с")
        if self.sros_enabled and not self.sros_keystore:
            errors.append("для SROS2 вкажіть шлях до ключів")
        return errors

    def namespace(self, camera_ns: str) -> str:
        return self.topic_prefix.rstrip("/") + "/" + camera_ns.strip("/")

    def env(self) -> Dict[str, str]:
        """Змінні оточення для процесу з ROS 2 (задаються до rclpy.init)."""
        env = {"ROS_DOMAIN_ID": str(self.domain_id), "RMW_IMPLEMENTATION": self.rmw}
        if self.discovery_server:
            env["ROS_DISCOVERY_SERVER"] = self.discovery_server
        if self.interface:
            # Fast DDS і Cyclone DDS задають інтерфейс по-різному
            if self.rmw == "rmw_cyclonedds_cpp":
                env["CYCLONEDDS_URI"] = (
                    f"<CycloneDDS><Domain><General><Interfaces><NetworkInterface name=\"{self.interface}\"/>"
                    "</Interfaces></General></Domain></CycloneDDS>")
            else:
                env["POLAR_DDS_INTERFACE"] = self.interface  # профіль Fast DDS — етап 9
        if self.sros_enabled:
            env.update(ROS_SECURITY_ENABLE="true", ROS_SECURITY_STRATEGY="Enforce",
                       ROS_SECURITY_KEYSTORE=self.sros_keystore)
        return env


# --- база даних --------------------------------------------------------------
_ROS_PREFIX = "ros."


def load_ros_settings(conn: sqlite3.Connection) -> RosSettings:
    rows = dict(conn.execute("SELECT key, value FROM settings WHERE key LIKE 'ros.%'").fetchall())
    s = RosSettings()
    for f in fields(RosSettings):
        raw = rows.get(_ROS_PREFIX + f.name)
        if raw is None:
            continue
        cur = getattr(s, f.name)
        if isinstance(cur, bool):
            val = raw in ("1", "true", "True")
        else:
            val = type(cur)(raw)
        setattr(s, f.name, val)
    return s


def save_ros_settings(conn: sqlite3.Connection, s: RosSettings) -> None:
    errors = s.validate()
    if errors:
        raise ValueError("; ".join(errors))
    with conn:
        for key, value in asdict(s).items():
            if isinstance(value, bool):
                value = int(value)
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (_ROS_PREFIX + key, str(value)))


_SERVO_COLS = ("servo_id", "direction", "zero_offset_deg", "gear_ratio", "max_speed_deg_s",
               "acceleration", "torque_limit_pct", "temperature_limit_c", "min_deg", "max_deg")


def host_from_url(url: str) -> str:
    """Хост з адреси потоку (rtsp://user@192.168.0.120:8554/x → 192.168.0.120); USB-пристрій — порожньо."""
    from urllib.parse import urlsplit
    if "://" not in url:
        return ""
    try:
        return urlsplit(url.strip()).hostname or ""
    except ValueError:
        return ""


def load_control_configs(conn: sqlite3.Connection) -> List[CameraControlConfig]:
    """Камери з налаштуваннями керування. Камера без простору імен ROS береться за назвою."""
    cams = conn.execute("SELECT id, name, ros_namespace, north_offset_deg, servo_port, servo_baudrate, "
                        "demo_servos, pi_host, stream_url FROM cameras ORDER BY id").fetchall()
    result = []
    for c in cams:
        cfg = CameraControlConfig(camera_id=c[0], name=c[1], namespace=c[2] or f"camera_{c[0]}",
                                  north_offset_deg=c[3], port=c[4], baudrate=c[5], demo=bool(c[6]),
                                  host=(c[7] or "").strip() or host_from_url(c[8] or ""))
        for row in conn.execute(f"SELECT axis, {', '.join(_SERVO_COLS)} FROM servo_settings "
                                "WHERE camera_id = ?", (c[0],)):
            setattr(cfg, row[0], ServoSettings(row[0], *row[1:]))
        result.append(cfg)
    return result


def save_control_config(conn: sqlite3.Connection, cfg: CameraControlConfig) -> None:
    errors = cfg.validate()
    if errors:
        raise ValueError("; ".join(errors))
    with conn:
        conn.execute("UPDATE cameras SET servo_port = ?, servo_baudrate = ?, updated_at = datetime('now') "
                     "WHERE id = ?", (cfg.port, cfg.baudrate, cfg.camera_id))
        for s in (cfg.pan, cfg.tilt):
            values = [getattr(s, c) for c in _SERVO_COLS]
            conn.execute(
                f"INSERT INTO servo_settings (camera_id, axis, {', '.join(_SERVO_COLS)}) "
                f"VALUES (?, ?, {', '.join('?' * len(_SERVO_COLS))}) "
                "ON CONFLICT(camera_id, axis) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in _SERVO_COLS),
                [cfg.camera_id, s.axis, *values])


def save_zero_offset(conn: sqlite3.Connection, camera_id: int, axis: str, zero_offset_deg: float) -> None:
    with conn:
        cur = conn.execute("UPDATE servo_settings SET zero_offset_deg = ? WHERE camera_id = ? AND axis = ?",
                           (zero_offset_deg, camera_id, axis))
        if cur.rowcount == 0:
            s = default_servo(axis)
            s.zero_offset_deg = zero_offset_deg
            conn.execute(f"INSERT INTO servo_settings (camera_id, axis, {', '.join(_SERVO_COLS)}) "
                         f"VALUES (?, ?, {', '.join('?' * len(_SERVO_COLS))})",
                         [camera_id, axis, *[getattr(s, c) for c in _SERVO_COLS]])


# --- стан камери --------------------------------------------------------------
@dataclass
class ServoTelemetry:
    servo_id: int = 0
    online: bool = False
    position_deg: float = 0.0
    speed_deg_s: float = 0.0
    load_pct: float = 0.0
    voltage_v: float = 0.0
    current_a: float = 0.0
    temperature_c: float = 0.0
    moving: bool = False


@dataclass
class CameraSnapshot:
    """Останнє повідомлення стану від Pi. Кути — кути камери (без зміщення від півночі)."""
    received_at: float
    azimuth_deg: float
    elevation_deg: float
    moving: bool
    error_flags: int
    pan: ServoTelemetry = field(default_factory=ServoTelemetry)
    tilt: ServoTelemetry = field(default_factory=ServoTelemetry)


@dataclass
class CameraStatus:
    name: str
    link_ok: bool
    azimuth_deg: Optional[float] = None     # від півночі
    elevation_deg: Optional[float] = None
    moving: bool = False
    error_flags: int = 0
    pan: Optional[ServoTelemetry] = None
    tilt: Optional[ServoTelemetry] = None
    age_s: Optional[float] = None

    @property
    def link_text(self) -> str:
        return "на звʼязку" if self.link_ok else "немає звʼязку"

    @property
    def error_text(self) -> str:
        return describe_errors(self.error_flags)


def camera_status(cfg: CameraControlConfig, snap: Optional[CameraSnapshot], now: float,
                  link_timeout_s: float) -> CameraStatus:
    if snap is None:
        return CameraStatus(cfg.name, link_ok=False)
    age = now - snap.received_at
    return CameraStatus(
        name=cfg.name,
        link_ok=age <= link_timeout_s and not snap.error_flags & ERROR_LINK_TIMEOUT,
        azimuth_deg=sensor_to_azimuth(snap.azimuth_deg, cfg.north_offset_deg),
        elevation_deg=snap.elevation_deg,
        moving=snap.moving,
        error_flags=snap.error_flags,
        pan=snap.pan, tilt=snap.tilt,
        age_s=age,
    )


def target_to_camera(cfg: CameraControlConfig, azimuth_deg: float, elevation_deg: float
                     ) -> Tuple[float, float]:
    """Азимут від півночі і кут місця → кути камери, обмежені межами з налаштувань."""
    az = azimuth_to_sensor(azimuth_deg, cfg.north_offset_deg)
    if not cfg.pan.continuous:
        az = cfg.pan.clamp(az)
    return az, cfg.tilt.clamp(elevation_deg)


def camera_to_north(cfg: CameraControlConfig, azimuth_deg: float) -> float:
    return wrap360(azimuth_deg + cfg.north_offset_deg)
