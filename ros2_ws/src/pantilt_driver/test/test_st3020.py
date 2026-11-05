import pytest

from pantilt_driver import st3020
from pantilt_driver.servo import DEG_PER_STEP


def status(servo_id, params=b"", error=0):
    body = bytes([servo_id, len(params) + 2, error]) + params
    return b"\xff\xff" + body + bytes([st3020.checksum(body)])


def test_build_ping_packet():
    # Приклад із документації протоколу STS: PING для ID 1 → FF FF 01 02 01 FB
    assert st3020.build_packet(1, st3020.INST_PING) == bytes.fromhex("ffff010201fb")


def test_parse_status_ok_and_errors():
    assert st3020.parse_status(status(3, b"\x01\x02"), 3) == b"\x01\x02"
    with pytest.raises(ValueError):
        st3020.parse_status(status(3), 4)
    bad = bytearray(status(3, b"\x01"))
    bad[-1] ^= 0xFF
    with pytest.raises(ValueError):
        st3020.parse_status(bytes(bad), 3)
    with pytest.raises(ValueError):
        st3020.parse_status(status(3, error=0x20), 3)


@pytest.mark.parametrize("value", [0, 1, 4095, -1, -4096, 30000, -30000])
def test_signed15_roundtrip(value):
    assert st3020.decode_signed15(st3020.encode_signed15(value)) == value


class FakeSerial:
    """Напівдуплексна шина з ехом власної команди."""

    def __init__(self, reply_for):
        self.reply_for = reply_for
        self.written = []
        self._rx = b""

    def reset_input_buffer(self):
        self._rx = b""

    def write(self, data):
        self.written.append(bytes(data))
        self._rx = bytes(data) + self.reply_for(bytes(data))

    def flush(self):
        pass

    def read(self, n):
        out, self._rx = self._rx[:n], self._rx[n:]
        return out

    def close(self):
        pass


def make_bus(reply_for):
    import threading
    bus = st3020.ST3020Bus.__new__(st3020.ST3020Bus)
    bus._serial = FakeSerial(reply_for)
    bus._lock = threading.Lock()
    return bus


def test_read_decodes_status_block():
    block = bytearray(st3020.STATUS_BLOCK_LEN)
    off = st3020.STATUS_BLOCK_START

    def put16(reg, v):
        block[reg - off] = v & 0xFF
        block[reg - off + 1] = v >> 8

    put16(st3020.REG_PRESENT_POSITION, st3020.encode_signed15(-1024))
    put16(st3020.REG_PRESENT_SPEED, 2048)
    put16(st3020.REG_PRESENT_LOAD, 125)
    block[st3020.REG_PRESENT_VOLTAGE - off] = 121
    block[st3020.REG_PRESENT_TEMPERATURE - off] = 41
    block[st3020.REG_MOVING - off] = 1
    put16(st3020.REG_PRESENT_CURRENT, 100)

    bus = make_bus(lambda pkt: status(pkt[2], bytes(block)))
    r = bus.read(5)
    assert r.raw_deg == pytest.approx(-90.0)
    assert r.speed_deg_s == pytest.approx(180.0)
    assert r.load_pct == pytest.approx(12.5)
    assert r.voltage_v == pytest.approx(12.1)
    assert r.temperature_c == 41
    assert r.moving
    assert r.current_a == pytest.approx(0.65)


def test_read_no_reply_is_none():
    assert make_bus(lambda pkt: b"").read(5) is None


def test_move_writes_goal_block():
    bus = make_bus(lambda pkt: status(pkt[2]))
    bus.move(2, -90.0, 180.0, 50)
    pkt = bus._serial.written[-1]
    assert pkt[2] == 2 and pkt[4] == st3020.INST_WRITE and pkt[5] == st3020.REG_ACCELERATION
    payload = pkt[6:-1]
    assert payload[0] == 50
    assert st3020.decode_signed15(st3020.u16(payload[1], payload[2])) == -1024
    assert st3020.u16(payload[5], payload[6]) == round(180.0 / DEG_PER_STEP)


def test_rebase_not_implemented():
    with pytest.raises(NotImplementedError):
        make_bus(lambda pkt: b"").rebase(1, 2)


def test_set_id_writes_unlock_id_lock():
    present = {1}

    def reply(pkt):
        sid, inst = pkt[2], pkt[4]
        if sid not in present:
            return b""
        if inst == st3020.INST_WRITE and pkt[5] == st3020.REG_ID:
            present.discard(sid)
            present.add(pkt[6])
        return status(sid)

    bus = make_bus(reply)
    bus.set_id(1, 9)
    assert present == {9}
    writes = [(p[2], p[5], p[6]) for p in bus._serial.written if p[4] == st3020.INST_WRITE]
    assert writes == [(1, st3020.REG_LOCK, 0), (1, st3020.REG_ID, 9), (9, st3020.REG_LOCK, 1)]
    with pytest.raises(ValueError):
        bus.set_id(9, 0)
    present.add(3)
    with pytest.raises(ValueError):
        bus.set_id(9, 3)          # зайнятий
