"""Інтерфейс доступу до сервоприводів і імітатор для роботи без обладнання."""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from .servo import ServoReading


class ServoBackend(abc.ABC):
    """Доступ до сервоприводів на шині. Усі кути — кути вала сервопривода (raw), у градусах."""

    @abc.abstractmethod
    def ping(self, servo_id: int) -> bool: ...

    @abc.abstractmethod
    def read(self, servo_id: int) -> Optional[ServoReading]:
        """None, якщо сервопривід не відповів."""

    @abc.abstractmethod
    def move(self, servo_id: int, raw_deg: float, speed_deg_s: float, acceleration: int) -> None: ...

    @abc.abstractmethod
    def stop(self, servo_id: int) -> None:
        """Зупинити в поточному положенні (утримуючи момент)."""

    @abc.abstractmethod
    def set_torque_limit(self, servo_id: int, pct: float) -> None: ...

    @abc.abstractmethod
    def rebase(self, servo_id: int, turns: int) -> None:
        """Зсунути лічильник обертів на `turns` повних обертів без руху вала.

        Потрібно для осі азимуту з ковзним контактом, щоб не вийти за межу
        багатооборотного режиму ST3020 (±7,5 оберту).
        """

    def close(self) -> None:  # noqa: B027 — необов'язковий метод
        pass


@dataclass
class _SimServo:
    raw_deg: float = 0.0
    goal_deg: float = 0.0
    speed_deg_s: float = 0.0
    torque_pct: float = 100.0
    online: bool = True
    temperature_c: float = 35.0


class SimBackend(ServoBackend):
    """Імітатор сервоприводів ST3020: рух з постійною швидкістю до цілі, синтетична телеметрія."""

    def __init__(self, servo_ids, clock: Callable[[], float] = time.monotonic,
                 voltage_v: float = 12.0) -> None:
        self._clock = clock
        self._t = clock()
        self._voltage = voltage_v
        self.servos: Dict[int, _SimServo] = {sid: _SimServo() for sid in servo_ids}

    def _advance(self) -> None:
        now = self._clock()
        dt = max(0.0, now - self._t)
        self._t = now
        for s in self.servos.values():
            diff = s.goal_deg - s.raw_deg
            step = s.speed_deg_s * dt
            if abs(diff) <= step:
                s.raw_deg = s.goal_deg
            else:
                s.raw_deg += step if diff > 0 else -step

    def ping(self, servo_id: int) -> bool:
        s = self.servos.get(servo_id)
        return bool(s and s.online)

    def read(self, servo_id: int) -> Optional[ServoReading]:
        self._advance()
        s = self.servos.get(servo_id)
        if s is None or not s.online:
            return None
        moving = abs(s.goal_deg - s.raw_deg) > 1e-6
        return ServoReading(
            raw_deg=s.raw_deg,
            speed_deg_s=s.speed_deg_s if moving else 0.0,
            load_pct=8.0 if moving else 2.0,
            voltage_v=self._voltage,
            current_a=0.35 if moving else 0.2,
            temperature_c=s.temperature_c,
            moving=moving,
        )

    def move(self, servo_id: int, raw_deg: float, speed_deg_s: float, acceleration: int) -> None:
        self._advance()
        s = self.servos[servo_id]
        s.goal_deg = raw_deg
        s.speed_deg_s = speed_deg_s

    def stop(self, servo_id: int) -> None:
        self._advance()
        s = self.servos[servo_id]
        s.goal_deg = s.raw_deg

    def set_torque_limit(self, servo_id: int, pct: float) -> None:
        self.servos[servo_id].torque_pct = pct

    def rebase(self, servo_id: int, turns: int) -> None:
        self._advance()
        s = self.servos[servo_id]
        s.raw_deg -= turns * 360.0
        s.goal_deg -= turns * 360.0
