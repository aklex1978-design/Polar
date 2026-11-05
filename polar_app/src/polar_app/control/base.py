"""Канал до камер: ROS 2 (реальні Raspberry Pi) або імітатор у тому ж процесі."""
from __future__ import annotations

import abc
from concurrent.futures import Future
from typing import Iterable, List, Optional, Tuple

from polar_core.control import CameraControlConfig, CameraSnapshot, RosSettings


class ControlBackend(abc.ABC):
    """Усі кути — кути камери (без зміщення від півночі). Методи не блокують:
    команди відправляються одразу, сервіси повертають Future."""

    @abc.abstractmethod
    def start(self, cameras: Iterable[CameraControlConfig], ros: RosSettings) -> None:
        """Підключитися до камер. Можна викликати повторно після зміни списку камер."""

    @abc.abstractmethod
    def command(self, name: str, azimuth_deg: float, elevation_deg: float, speed_deg_s: float) -> None: ...

    @abc.abstractmethod
    def jog(self, name: str, d_azimuth_deg: float, d_elevation_deg: float, speed_deg_s: float) -> None: ...

    @abc.abstractmethod
    def stop(self, name: str) -> Future: ...

    @abc.abstractmethod
    def configure(self, cfg: CameraControlConfig, ros: RosSettings) -> "Future[Tuple[bool, str]]": ...

    @abc.abstractmethod
    def scan(self, name: str, first_id: int = 1, last_id: int = 253) -> "Future[List[int]]": ...

    @abc.abstractmethod
    def set_zero(self, name: str, axis: str) -> "Future[float]": ...

    @abc.abstractmethod
    def set_servo_id(self, name: str, old_id: int, new_id: int) -> "Future[Tuple[bool, str]]": ...

    @abc.abstractmethod
    def snapshot(self, name: str) -> Optional[CameraSnapshot]: ...

    def shutdown(self) -> None:  # noqa: B027
        pass


def done(value) -> Future:
    f: Future = Future()
    f.set_result(value)
    return f


def failed(exc: BaseException) -> Future:
    f: Future = Future()
    f.set_exception(exc)
    return f
