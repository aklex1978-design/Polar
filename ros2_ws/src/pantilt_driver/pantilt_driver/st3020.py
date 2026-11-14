"""Шина сервоприводів ST3020 (UART, напівдуплекс) через Bus Servo Driver HAT (A).

Протокол — пакети Feetech STS: 0xFF 0xFF ID LEN INSTR PARAMS... CHECKSUM.
Адреси регістрів нижче взято з таблиці пам'яті серії STS/ST; ПЕРЕД ПЕРШИМ ЗАПУСКОМ НА ОБЛАДНАННІ
їх треба звірити з таблицею регістрів ST3020 від Waveshare (позначено TODO: verify).
"""
from __future__ import annotations

import threading
from typing import Optional

from .backend import ServoBackend
from .servo import DEG_PER_STEP, ServoReading

# Інструкції
INST_PING = 0x01
INST_READ = 0x02
INST_WRITE = 0x03

# Регістри (TODO: verify для ST3020)
REG_ID = 5                    # 1 байт, EEPROM
REG_LOCK = 55                 # 1 байт: 0 — запис в EEPROM дозволено, 1 — заблоковано
REG_TORQUE_ENABLE = 40       # 1 байт: 0 вимк., 1 увімк.
REG_ACCELERATION = 41        # 1 байт
REG_GOAL_POSITION = 42       # 2 байти, кроки; біт 15 — знак (багатооборотний режим)
REG_GOAL_TIME = 44           # 2 байти
REG_GOAL_SPEED = 46          # 2 байти, кроків/с
REG_TORQUE_LIMIT = 48        # 2 байти, 0..1000 (0,1 %)
REG_PRESENT_POSITION = 56    # 2 байти
REG_PRESENT_SPEED = 58       # 2 байти
REG_PRESENT_LOAD = 60        # 2 байти, 0,1 %
REG_PRESENT_VOLTAGE = 62     # 1 байт, 0,1 В
REG_PRESENT_TEMPERATURE = 63  # 1 байт, °C
REG_MOVING = 66              # 1 байт
REG_PRESENT_CURRENT = 69     # 2 байти, 6,5 мА
STATUS_BLOCK_START = REG_PRESENT_POSITION
STATUS_BLOCK_LEN = REG_PRESENT_CURRENT + 2 - REG_PRESENT_POSITION  # 15 байт

CURRENT_UNIT_A = 0.0065


def checksum(body: bytes) -> int:
    return (~sum(body)) & 0xFF


def build_packet(servo_id: int, instruction: int, params: bytes = b"") -> bytes:
    body = bytes([servo_id, len(params) + 2, instruction]) + params
    return b"\xff\xff" + body + bytes([checksum(body)])


def parse_status(packet: bytes, expected_id: int) -> bytes:
    """Розібрати відповідь сервопривода, повернути параметри. ValueError — пошкоджений пакет."""
    if len(packet) < 6 or packet[0:2] != b"\xff\xff":
        raise ValueError("немає заголовка 0xFF 0xFF")
    servo_id, length, error = packet[2], packet[3], packet[4]
    if servo_id != expected_id:
        raise ValueError(f"відповідь від ID {servo_id}, очікувався {expected_id}")
    if len(packet) != 4 + length:
        raise ValueError("довжина пакета не збігається")
    body = packet[2:-1]
    if checksum(body) != packet[-1]:
        raise ValueError("неправильна контрольна сума")
    if error:
        raise ValueError(f"сервопривід повідомив про помилку 0x{error:02x}")
    return packet[5:-1]


def encode_signed15(value: int) -> int:
    """Знакове значення → формат ST (біт 15 — знак, решта — модуль)."""
    if value < 0:
        return (-value & 0x7FFF) | 0x8000
    return value & 0x7FFF


def decode_signed15(raw: int) -> int:
    return -(raw & 0x7FFF) if raw & 0x8000 else raw & 0x7FFF


def u16(lo: int, hi: int) -> int:
    return lo | (hi << 8)


class ST3020Bus(ServoBackend):
    """Сервоприводи ST3020 на одному UART. Потребує pyserial."""

    def __init__(self, port: str, baudrate: int = 1_000_000, timeout_s: float = 0.02) -> None:
        import serial  # імпорт тут, щоб тести й імітатор не потребували pyserial

        self._serial = serial.Serial(port, baudrate=baudrate, timeout=timeout_s)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._serial.close()

    # --- низький рівень ----------------------------------------------------
    def _transact(self, servo_id: int, instruction: int, params: bytes = b"",
                  expect_reply: bool = True) -> Optional[bytes]:
        packet = build_packet(servo_id, instruction, params)
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write(packet)
            self._serial.flush()
            if not expect_reply:
                return None
            # Напівдуплекс: на деяких платах у вхідному буфері з'являється власна команда — пропускаємо її.
            data = self._serial.read(len(packet) + 64)
        idx = data.find(b"\xff\xff", len(packet) if data.startswith(packet) else 0)
        if idx < 0:
            return None
        if len(data) < idx + 4:
            return None
        length = data[idx + 3]
        reply = data[idx: idx + 4 + length]
        try:
            return parse_status(reply, servo_id)
        except ValueError:
            return None

    def _write(self, servo_id: int, address: int, payload: bytes) -> bool:
        return self._transact(servo_id, INST_WRITE, bytes([address]) + payload) is not None

    def _read(self, servo_id: int, address: int, length: int) -> Optional[bytes]:
        data = self._transact(servo_id, INST_READ, bytes([address, length]))
        if data is None or len(data) != length:
            return None
        return data

    # --- ServoBackend ------------------------------------------------------
    def ping(self, servo_id: int) -> bool:
        return self._transact(servo_id, INST_PING) is not None

    def read(self, servo_id: int) -> Optional[ServoReading]:
        b = self._read(servo_id, STATUS_BLOCK_START, STATUS_BLOCK_LEN)
        if b is None:
            return None
        off = STATUS_BLOCK_START

        def at(reg: int) -> int:
            return b[reg - off]

        position = decode_signed15(u16(at(REG_PRESENT_POSITION), at(REG_PRESENT_POSITION + 1)))
        speed = decode_signed15(u16(at(REG_PRESENT_SPEED), at(REG_PRESENT_SPEED + 1)))
        # навантаження: біти 0–9 — модуль 0…1000 (0,1 %), біт 10 — напрямок (перевірено на ST3020:
        # 1064 = −4,0 %, а не 106,4 %)
        raw_load = u16(at(REG_PRESENT_LOAD), at(REG_PRESENT_LOAD + 1))
        load = -(raw_load & 0x3FF) if raw_load & 0x400 else raw_load & 0x3FF
        current = u16(at(REG_PRESENT_CURRENT), at(REG_PRESENT_CURRENT + 1)) & 0x7FFF
        return ServoReading(
            raw_deg=position * DEG_PER_STEP,
            speed_deg_s=speed * DEG_PER_STEP,
            load_pct=load / 10.0,
            voltage_v=at(REG_PRESENT_VOLTAGE) / 10.0,
            current_a=current * CURRENT_UNIT_A,
            temperature_c=float(at(REG_PRESENT_TEMPERATURE)),
            moving=bool(at(REG_MOVING)),
        )

    def move(self, servo_id: int, raw_deg: float, speed_deg_s: float, acceleration: int) -> None:
        steps = int(round(raw_deg / DEG_PER_STEP))
        speed = max(1, min(0x7FFF, int(round(speed_deg_s / DEG_PER_STEP))))
        pos = encode_signed15(steps)
        payload = bytes([
            max(0, min(254, acceleration)),   # 41 прискорення
            pos & 0xFF, pos >> 8,             # 42-43 ціль
            0, 0,                             # 44-45 час
            speed & 0xFF, speed >> 8,         # 46-47 швидкість
        ])
        self._write(servo_id, REG_ACCELERATION, payload)

    def stop(self, servo_id: int) -> None:
        reading = self.read(servo_id)
        if reading is not None:
            self.move(servo_id, reading.raw_deg, 360.0, 254)

    def set_torque_limit(self, servo_id: int, pct: float) -> None:
        value = int(round(max(0.0, min(100.0, pct)) * 10))
        self._write(servo_id, REG_TORQUE_LIMIT, bytes([value & 0xFF, value >> 8]))

    def set_id(self, old_id: int, new_id: int) -> None:
        # адреси ID (5) і блокування EEPROM (55) перевірено на ST3020 (зміна ID 1 → 2)
        if not 1 <= new_id <= 253:
            raise ValueError("ID має бути 1..253")
        if self.ping(new_id):
            raise ValueError(f"ID {new_id} вже зайнятий на шині")
        if not self._write(old_id, REG_LOCK, bytes([0])):
            raise IOError(f"сервопривід {old_id} не відповідає")
        self._write(old_id, REG_ID, bytes([new_id]))
        self._write(new_id, REG_LOCK, bytes([1]))
        if not self.ping(new_id):
            raise IOError(f"після зміни сервопривід не відповідає з ID {new_id}")

    def rebase(self, servo_id: int, turns: int) -> None:
        # TODO: verify — спосіб скинути лічильник обертів без руху вала залежить від прошивки ST3020
        # (калібрування середньої точки / регістр зміщення). До перевірки на обладнанні — не підтримано.
        raise NotImplementedError(
            "Скидання лічильника обертів ST3020 ще не перевірено на обладнанні"
        )
