"""polar_control: керування камерами на сервері — база даних + канал до камер + правила з ТЗ."""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import Future
from typing import Callable, Dict, List, Optional, Tuple

from polar_core import control as ctl
from polar_core.control import CameraControlConfig, CameraStatus, ManualControlLocked, RosSettings

from .base import ControlBackend


class ControlService:
    """Те, з чим працює інтерфейс: кути від півночі, межі, стан звʼязку, блокування під час Навчання.

    Налаштування сервоприводів надсилаються на Pi після збереження і щоразу, коли камера
    (знову) виходить на звʼязок — Pi міг перезавантажитися зі значеннями за замовчуванням.
    """

    def __init__(self, conn: sqlite3.Connection, backend: ControlBackend,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.conn = conn
        self.backend = backend
        self._clock = clock
        self._db_lock = threading.RLock()     # повторний вхід з того самого потоку — без взаємоблокування
        self._learning: Dict[str, bool] = {}
        self._was_online: Dict[str, bool] = {}
        self._pending_configure: Dict[str, Future] = {}
        self.last_configure: Dict[str, Tuple[bool, str]] = {}
        self.reload()

    # --- налаштування -------------------------------------------------------
    def reload(self) -> None:
        with self._db_lock:
            self.ros: RosSettings = ctl.load_ros_settings(self.conn)
            self._cams: Dict[str, CameraControlConfig] = {
                c.name: c for c in ctl.load_control_configs(self.conn)}

    def start(self) -> None:
        self.backend.start(self._cams.values(), self.ros)

    def shutdown(self) -> None:
        self.backend.shutdown()

    def camera_names(self) -> List[str]:
        return list(self._cams)

    def config(self, name: str) -> CameraControlConfig:
        return self._cams[name]

    def save_config(self, cfg: CameraControlConfig) -> Future:
        """Зберегти налаштування сервоприводів у базу і надіслати на Pi."""
        with self._db_lock:
            ctl.save_control_config(self.conn, cfg)   # ValueError — помилки перевірки
        self._cams[cfg.name] = cfg
        return self._push_config(cfg.name)

    def save_ros_settings(self, ros: RosSettings) -> None:
        with self._db_lock:
            ctl.save_ros_settings(self.conn, ros)
        self.ros = ros
        self.start()

    def _push_config(self, name: str) -> Future:
        f = self.backend.configure(self._cams[name], self.ros)
        self._pending_configure[name] = f

        def remember(fut: Future):
            try:
                self.last_configure[name] = fut.result()
            except Exception as e:  # noqa: BLE001
                self.last_configure[name] = (False, str(e))
        f.add_done_callback(remember)
        return f

    # --- стан ------------------------------------------------------------------
    def status(self, name: str) -> CameraStatus:
        st = ctl.camera_status(self._cams[name], self.backend.snapshot(name), self._clock(),
                               self.ros.link_timeout_s)
        was = self._was_online.get(name, False)
        self._was_online[name] = st.link_ok
        if st.link_ok and not was:
            pending = self._pending_configure.get(name)
            if pending is None or pending.done():
                self._push_config(name)
        return st

    def statuses(self) -> List[CameraStatus]:
        return [self.status(n) for n in self._cams]

    # --- Навчання -----------------------------------------------------------
    def set_learning(self, name: str, on: bool) -> None:
        self._learning[name] = on

    def is_learning(self, name: str) -> bool:
        return self._learning.get(name, False)

    def _check_manual(self, name: str) -> None:
        if self.is_learning(name):
            raise ManualControlLocked(f"{name}: під час Навчання ручне керування заблоковане")

    # --- команди --------------------------------------------------------------
    def _speed(self, name: str, speed_deg_s: float) -> float:
        cfg = self._cams[name]
        top = min(cfg.pan.max_speed_deg_s, cfg.tilt.max_speed_deg_s)
        return top if speed_deg_s <= 0 else min(speed_deg_s, top)

    def go_to(self, name: str, azimuth_deg: float, elevation_deg: float, speed_deg_s: float = 0.0,
              source: str = "manual") -> Tuple[float, float]:
        """Азимут від півночі і кут місця. Повертає ціль після обмеження межами (від півночі)."""
        if source in ("manual", "measure"):
            self._check_manual(name)
        cfg = self._cams[name]
        az_cam, el = ctl.target_to_camera(cfg, azimuth_deg, elevation_deg)
        self.backend.command(name, az_cam, el, self._speed(name, speed_deg_s))
        return ctl.camera_to_north(cfg, az_cam), el

    def jog(self, name: str, d_azimuth_deg: float, d_elevation_deg: float, speed_deg_s: float = 0.0) -> None:
        self._check_manual(name)
        self.backend.jog(name, d_azimuth_deg, d_elevation_deg, self._speed(name, speed_deg_s))

    def stop(self, name: str) -> Future:
        """«Стоп» працює завжди, зокрема під час Навчання."""
        self._learning[name] = False
        return self.backend.stop(name)

    def stop_all(self) -> List[Future]:
        return [self.stop(n) for n in self._cams]

    def scan(self, name: str, first_id: int = 1, last_id: int = 253) -> Future:
        return self.backend.scan(name, first_id, last_id)

    def set_servo_id(self, name: str, old_id: int, new_id: int) -> Future:
        return self.backend.set_servo_id(name, old_id, new_id)

    def set_zero(self, name: str, axis: str) -> Future:
        """Поточне положення осі стає нулем; нове зміщення записується в базу."""
        cfg = self._cams[name]
        out: Future = Future()

        def saved(f: Future):
            try:
                value = f.result()
                with self._db_lock:
                    ctl.save_zero_offset(self.conn, cfg.camera_id, axis, value)
                getattr(cfg, axis).zero_offset_deg = value
                out.set_result(value)
            except Exception as e:  # noqa: BLE001
                out.set_exception(e)
        self.backend.set_zero(name, axis).add_done_callback(saved)
        return out
