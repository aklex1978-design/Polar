"""Логіка керування камерою (два сервоприводи) без залежності від ROS — тестується окремо."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .backend import ServoBackend
from .servo import (
    MULTITURN_LIMIT_DEG,
    AxisStatus,
    ServoConfig,
    shortest_target,
    wrap360,
)

ERROR_NONE = 0
ERROR_SERVO_OFFLINE = 1
ERROR_OVERHEAT = 2
ERROR_LINK_TIMEOUT = 4
ERROR_STOPPED = 8

# Коли розгорнутий кут азимуту відійде від нуля більше ніж на стільки,
# лічильник обертів скидається (до межі ±7,5 оберту лишається запас).
REBASE_THRESHOLD_DEG = MULTITURN_LIMIT_DEG - 2 * 360.0


@dataclass
class CameraState:
    stamp: float = 0.0
    azimuth_deg: float = 0.0
    elevation_deg: float = 0.0
    moving: bool = False
    error_flags: int = ERROR_NONE
    pan: AxisStatus = field(default_factory=AxisStatus)
    tilt: AxisStatus = field(default_factory=AxisStatus)


class PanTiltController:
    """Керує горизонтальним (pan, азимут) і вертикальним (tilt, кут місця) сервоприводами."""

    def __init__(self, backend: ServoBackend, pan: ServoConfig, tilt: ServoConfig,
                 link_timeout_s: float = 2.0, clock: Callable[[], float] = time.monotonic) -> None:
        if pan.servo_id == tilt.servo_id:
            raise ValueError("Горизонтальний і вертикальний сервоприводи мають мати різні ID")
        self.backend = backend
        self.pan = pan
        self.tilt = tilt
        self.link_timeout_s = link_timeout_s
        self._clock = clock
        self._last_link = clock()
        self._link_lost = False
        self._stopped = False
        self.state = CameraState()
        for cfg in (pan, tilt):
            backend.set_torque_limit(cfg.servo_id, cfg.torque_limit_pct)
        self.update()

    def reconfigure(self, pan: ServoConfig, tilt: ServoConfig,
                    link_timeout_s: Optional[float] = None) -> None:
        """Застосувати нові налаштування сервоприводів без перезапуску."""
        if pan.servo_id == tilt.servo_id:
            raise ValueError("Горизонтальний і вертикальний сервоприводи мають мати різні ID")
        self._halt()
        self.pan, self.tilt = pan, tilt
        if link_timeout_s is not None:
            self.link_timeout_s = link_timeout_s
        for cfg in (pan, tilt):
            self.backend.set_torque_limit(cfg.servo_id, cfg.torque_limit_pct)
        self.update()

    def zero_here(self, axis: str) -> float:
        """Поточне положення осі стає нулем камери. Повертає нове зміщення нуля (кут вала)."""
        cfg = {"pan": self.pan, "tilt": self.tilt}.get(axis)
        if cfg is None:
            raise ValueError("axis має бути 'pan' або 'tilt'")
        reading = self.backend.read(cfg.servo_id)
        if reading is None:
            raise IOError(f"сервопривід {cfg.servo_id} не відповідає")
        # Для азимуту — у межах одного оберту: кількість обертів після вимкнення не зберігається.
        cfg.zero_offset_deg = wrap360(reading.raw_deg) if cfg.continuous else reading.raw_deg
        self.update()
        return cfg.zero_offset_deg

    # --- команди від сервера ------------------------------------------------
    def heartbeat(self) -> None:
        """Сервер на зв'язку."""
        self._last_link = self._clock()
        self._link_lost = False

    def command(self, azimuth_deg: float, elevation_deg: float, speed_deg_s: float = 0.0) -> None:
        """Перейти в задане положення камери."""
        self.heartbeat()
        self._stopped = False
        self._move_pan(azimuth_deg, speed_deg_s)
        self._move_tilt(elevation_deg, speed_deg_s)

    def jog(self, d_azimuth_deg: float, d_elevation_deg: float, speed_deg_s: float = 0.0) -> None:
        """Крок відносно поточного положення."""
        self.heartbeat()
        self._stopped = False
        if d_azimuth_deg:
            self._move_pan(self.state.azimuth_deg + d_azimuth_deg, speed_deg_s)
        if d_elevation_deg:
            self._move_tilt(self.state.elevation_deg + d_elevation_deg, speed_deg_s)

    def stop(self) -> None:
        """Негайна зупинка обох сервоприводів (кнопка «Стоп»)."""
        self._stopped = True
        self._halt()

    def _halt(self) -> None:
        for cfg in (self.pan, self.tilt):
            self.backend.stop(cfg.servo_id)

    # --- рух осей ----------------------------------------------------------
    def _move_pan(self, azimuth_deg: float, speed_deg_s: float) -> None:
        cfg = self.pan
        if cfg.continuous:
            target_axis = shortest_target(self.state.pan.unwrapped_deg, wrap360(azimuth_deg))
        else:
            target_axis = cfg.clamp_axis(wrap360(azimuth_deg))
        self.backend.move(cfg.servo_id, cfg.axis_to_raw(target_axis),
                          cfg.limit_speed(speed_deg_s) * cfg.gear_ratio, cfg.acceleration)

    def _move_tilt(self, elevation_deg: float, speed_deg_s: float) -> None:
        cfg = self.tilt
        target_axis = cfg.clamp_axis(elevation_deg)
        self.backend.move(cfg.servo_id, cfg.axis_to_raw(target_axis),
                          cfg.limit_speed(speed_deg_s) * cfg.gear_ratio, cfg.acceleration)

    # --- цикл опитування ---------------------------------------------------
    def _read_axis(self, cfg: ServoConfig, status: AxisStatus) -> None:
        reading = self.backend.read(cfg.servo_id)
        status.reading = reading
        status.online = reading is not None
        if reading is None:
            return
        unwrapped = cfg.raw_to_axis(reading.raw_deg)
        status.unwrapped_deg = unwrapped
        status.axis_deg = wrap360(unwrapped) if cfg.continuous else unwrapped
        status.overheated = reading.temperature_c >= cfg.temperature_limit_c

    def update(self) -> CameraState:
        """Опитати сервоприводи, перевірити зв'язок і перегрів. Викликається з частотою стану."""
        now = self._clock()
        st = self.state
        self._read_axis(self.pan, st.pan)
        self._read_axis(self.tilt, st.tilt)

        if not self._link_lost and now - self._last_link > self.link_timeout_s:
            self._link_lost = True
            self._halt()

        st.moving = any(s.reading is not None and s.reading.moving for s in (st.pan, st.tilt))
        flags = ERROR_NONE
        if not (st.pan.online and st.tilt.online):
            flags |= ERROR_SERVO_OFFLINE
        if st.pan.overheated or st.tilt.overheated:
            flags |= ERROR_OVERHEAT
            if st.moving:
                self._halt()
        if self._link_lost:
            flags |= ERROR_LINK_TIMEOUT
        if self._stopped:
            flags |= ERROR_STOPPED

        st.stamp = now
        st.azimuth_deg = st.pan.axis_deg
        st.elevation_deg = st.tilt.axis_deg
        st.error_flags = flags

        self._maybe_rebase()
        return st

    def _maybe_rebase(self) -> None:
        """Скинути лічильник обертів азимуту, коли камера стоїть і пішла далеко від нуля."""
        cfg, pan = self.pan, self.state.pan
        if not cfg.continuous or pan.reading is None or pan.reading.moving:
            return
        raw_from_zero = pan.reading.raw_deg - cfg.zero_offset_deg
        if abs(raw_from_zero) < REBASE_THRESHOLD_DEG:
            return
        turns = int(raw_from_zero // 360.0)
        if turns:
            try:
                self.backend.rebase(cfg.servo_id, turns)
            except NotImplementedError:
                return  # бекенд не вміє — камера й далі працює в межах ±7,5 оберту
            pan.unwrapped_deg -= cfg.direction * turns * 360.0 / cfg.gear_ratio
