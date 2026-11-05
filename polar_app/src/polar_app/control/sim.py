"""Імітатор камер у процесі програми: та сама логіка, що й у pantilt_driver на Raspberry Pi."""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Dict, Iterable, Optional

from pantilt_driver.backend import SimBackend
from pantilt_driver.controller import PanTiltController
from pantilt_driver.servo import ServoConfig
from polar_core.control import (CameraControlConfig, CameraSnapshot, RosSettings, ServoSettings,
                                ServoTelemetry)

from .base import ControlBackend, failed


def to_servo_config(s: ServoSettings) -> ServoConfig:
    return ServoConfig(servo_id=s.servo_id, direction=s.direction, zero_offset_deg=s.zero_offset_deg,
                       gear_ratio=s.gear_ratio, max_speed_deg_s=s.max_speed_deg_s,
                       acceleration=s.acceleration, torque_limit_pct=s.torque_limit_pct,
                       temperature_limit_c=s.temperature_limit_c, min_deg=s.min_deg, max_deg=s.max_deg,
                       continuous=s.continuous)


class _SimCamera:
    def __init__(self, cfg: CameraControlConfig, ros: RosSettings, clock) -> None:
        self.bus = SimBackend([cfg.pan.servo_id, cfg.tilt.servo_id], clock=clock)
        # імітуємо, що вал стоїть там, де камера має 0° (інакше зміщення нуля «крутить» камеру на старті)
        for s in (cfg.pan, cfg.tilt):
            self.bus.servos[s.servo_id].raw_deg = self.bus.servos[s.servo_id].goal_deg = s.zero_offset_deg
        self.ctrl = PanTiltController(self.bus, to_servo_config(cfg.pan), to_servo_config(cfg.tilt),
                                      link_timeout_s=ros.link_timeout_s, clock=clock)
        self.online = True               # False — імітація обриву мережі до Pi
        self.snapshot: Optional[CameraSnapshot] = None
        self.lock = threading.Lock()


class SimControlBackend(ControlBackend):
    def __init__(self, clock: Callable[[], float] = time.monotonic, autostart_thread: bool = True) -> None:
        self._clock = clock
        self._cams: Dict[str, _SimCamera] = {}
        self._rate_hz = 20.0
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sim-srv")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._autostart = autostart_thread

    # --- життєвий цикл ----------------------------------------------------
    def start(self, cameras: Iterable[CameraControlConfig], ros: RosSettings) -> None:
        self._rate_hz = ros.state_rate_hz
        cams = {}
        for cfg in cameras:
            cams[cfg.name] = self._cams.get(cfg.name) or _SimCamera(cfg, ros, self._clock)
        self._cams = cams
        if self._autostart and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="sim-cameras", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(1.0 / self._rate_hz):
            self.tick()

    def tick(self) -> None:
        """Один цикл: сервер «на звʼязку» → Pi оновлює стан → сервер отримує стан."""
        for cam in list(self._cams.values()):
            with cam.lock:
                if cam.online:
                    cam.ctrl.heartbeat()
                st = cam.ctrl.update()
                if not cam.online:
                    continue
                cam.snapshot = CameraSnapshot(
                    received_at=self._clock(), azimuth_deg=st.azimuth_deg, elevation_deg=st.elevation_deg,
                    moving=st.moving, error_flags=st.error_flags,
                    pan=self._telemetry(cam.ctrl.pan.servo_id, st.pan),
                    tilt=self._telemetry(cam.ctrl.tilt.servo_id, st.tilt))

    @staticmethod
    def _telemetry(servo_id, status) -> ServoTelemetry:
        t = ServoTelemetry(servo_id=servo_id, online=status.online, position_deg=status.axis_deg)
        r = status.reading
        if r is not None:
            t.speed_deg_s, t.load_pct, t.voltage_v = r.speed_deg_s, r.load_pct, r.voltage_v
            t.current_a, t.temperature_c, t.moving = r.current_a, r.temperature_c, r.moving
        return t

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._pool.shutdown(wait=False)

    # --- імітація несправностей (для перевірок і демонстрації) ------------
    def set_online(self, name: str, online: bool) -> None:
        self._cams[name].online = online

    def camera(self, name: str) -> _SimCamera:
        return self._cams[name]

    # --- команди ----------------------------------------------------------
    def _call(self, name: str, fn) -> Future:
        cam = self._cams.get(name)
        if cam is None:
            return failed(KeyError(name))
        if not cam.online:
            return failed(TimeoutError(f"{name}: немає звʼязку"))

        def run():
            with cam.lock:
                return fn(cam)
        return self._pool.submit(run)

    def command(self, name, azimuth_deg, elevation_deg, speed_deg_s) -> None:
        self._call(name, lambda c: c.ctrl.command(azimuth_deg, elevation_deg, speed_deg_s))

    def jog(self, name, d_azimuth_deg, d_elevation_deg, speed_deg_s) -> None:
        self._call(name, lambda c: c.ctrl.jog(d_azimuth_deg, d_elevation_deg, speed_deg_s))

    def stop(self, name) -> Future:
        return self._call(name, lambda c: c.ctrl.stop())

    def configure(self, cfg: CameraControlConfig, ros: RosSettings) -> Future:
        def run(c: _SimCamera):
            try:
                c.ctrl.reconfigure(to_servo_config(cfg.pan), to_servo_config(cfg.tilt), ros.link_timeout_s)
            except ValueError as e:
                return False, str(e)
            self._rate_hz = ros.state_rate_hz
            return True, "налаштування застосовано"
        return self._call(cfg.name, run)

    def scan(self, name, first_id=1, last_id=253) -> Future:
        return self._call(name, lambda c: c.bus.scan(range(first_id, last_id + 1)))

    def set_zero(self, name, axis) -> Future:
        return self._call(name, lambda c: c.ctrl.zero_here(axis))

    def set_servo_id(self, name, old_id, new_id) -> Future:
        def run(c):
            try:
                c.bus.set_id(old_id, new_id)
                return True, f"ID {old_id} → {new_id}"
            except (KeyError, ValueError) as e:
                return False, str(e)
        return self._call(name, run)

    def snapshot(self, name) -> Optional[CameraSnapshot]:
        cam = self._cams.get(name)
        return cam.snapshot if cam else None
