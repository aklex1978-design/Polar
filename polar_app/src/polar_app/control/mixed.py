"""Канал до камер з демо окремо для кожної камери: камери з увімкненим «Демо сервоприводів»
керуються імітатором, решта — через ROS 2 (справжні Raspberry Pi)."""
from __future__ import annotations

from concurrent.futures import Future
from typing import Callable, Dict, Iterable, Optional

from polar_core.control import CameraControlConfig, CameraSnapshot, RosSettings

from .base import ControlBackend, failed
from .sim import SimControlBackend


def _default_ros():
    from .ros import RosControlBackend

    return RosControlBackend()


class MixedControlBackend(ControlBackend):
    def __init__(self, ros_factory: Callable[[], ControlBackend] = _default_ros,
                 sim: Optional[SimControlBackend] = None) -> None:
        self.sim = sim or SimControlBackend()
        self.sim.add_state_listener(self._emit)
        self._ros_factory = ros_factory
        self.ros: Optional[ControlBackend] = None
        self.ros_error = ""              # чому ROS 2 недоступний (напр. не встановлено на цьому компʼютері)
        self._demo: Dict[str, bool] = {}

    def is_demo(self, name: str) -> bool:
        return self._demo.get(name, False)

    def start(self, cameras: Iterable[CameraControlConfig], ros: RosSettings) -> None:
        cams = list(cameras)
        self._demo = {c.name: c.demo for c in cams}
        self.sim.start([c for c in cams if c.demo], ros)
        real = [c for c in cams if not c.demo]
        if real and self.ros is None and not self.ros_error:
            try:
                self.ros = self._ros_factory()
                self.ros.add_state_listener(self._emit)
            except Exception as e:  # noqa: BLE001 — rclpy або polar_interfaces не встановлено
                self.ros_error = f"ROS 2 недоступний: {e}"
        if self.ros is not None:
            self.ros.start(real, ros)

    def _backend(self, name: str) -> Optional[ControlBackend]:
        return self.sim if self.is_demo(name) else self.ros

    def _no_ros(self, name: str) -> Exception:
        return RuntimeError(f"{name}: {self.ros_error or 'ROS 2 не запущено'}")

    def command(self, name, azimuth_deg, elevation_deg, speed_deg_s) -> None:
        b = self._backend(name)
        if b is None:
            raise self._no_ros(name)
        b.command(name, azimuth_deg, elevation_deg, speed_deg_s)

    def jog(self, name, d_azimuth_deg, d_elevation_deg, speed_deg_s) -> None:
        b = self._backend(name)
        if b is None:
            raise self._no_ros(name)
        b.jog(name, d_azimuth_deg, d_elevation_deg, speed_deg_s)

    def _call(self, name, method, *args) -> Future:
        b = self._backend(name)
        return failed(self._no_ros(name)) if b is None else getattr(b, method)(*args)

    def stop(self, name) -> Future:
        return self._call(name, "stop", name)

    def configure(self, cfg, ros) -> Future:
        return self._call(cfg.name, "configure", cfg, ros)

    def scan(self, name, first_id=1, last_id=253) -> Future:
        return self._call(name, "scan", name, first_id, last_id)

    def set_zero(self, name, axis) -> Future:
        return self._call(name, "set_zero", name, axis)

    def set_servo_id(self, name, old_id, new_id) -> Future:
        return self._call(name, "set_servo_id", name, old_id, new_id)

    def snapshot(self, name) -> Optional[CameraSnapshot]:
        b = self._backend(name)
        return b.snapshot(name) if b is not None else None

    def shutdown(self) -> None:
        self.sim.shutdown()
        if self.ros is not None:
            self.ros.shutdown()
