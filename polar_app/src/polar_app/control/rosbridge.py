"""Канал до камер через rosbridge: Polar на Windows без ROS 2.

На кожному Raspberry Pi працює rosbridge_server (WebSocket, порт 9090 за замовчуванням) поруч із вузлом
pantilt_driver. Polar підключається до нього по мережі й обмінюється JSON-повідомленнями протоколу rosbridge:
публікує cmd, jog, heartbeat; підписаний на state; викликає сервіси stop, configure, scan, set_zero,
set_servo_id. Топіки й сервіси ті самі, що й у RosControlBackend (DDS), — Pi нічого не міняє.

Клієнт WebSocket — лише стандартна бібліотека Python (RFC 6455, без розширень): інсталятору Polar
не потрібні додаткові пакети. Зʼєднання з кожним Pi відновлюється само; після відновлення підписки й
оголошення топіків повторюються.
"""
from __future__ import annotations

import base64
import hashlib
import itertools
import json
import logging
import os
import socket
import struct
import threading
import time
from concurrent.futures import Future
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from polar_core.control import (CameraControlConfig, CameraSnapshot, RosSettings, ServoSettings,
                                ServoTelemetry)

from .base import ControlBackend, failed

log = logging.getLogger(__name__)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
T_CMD = "polar_interfaces/msg/PanTiltCommand"
T_JOG = "polar_interfaces/msg/PanTiltJog"
T_STATE = "polar_interfaces/msg/PanTiltState"
T_EMPTY = "std_msgs/msg/Empty"
S_TRIGGER = "std_srvs/srv/Trigger"
S_CONFIGURE = "polar_interfaces/srv/ConfigureServos"
S_SCAN = "polar_interfaces/srv/ScanServos"
S_SET_ZERO = "polar_interfaces/srv/SetZero"
S_SET_ID = "polar_interfaces/srv/SetServoId"


# --- WebSocket (RFC 6455), лише клієнт --------------------------------------------------------------
class WebSocket:
    """Мінімальний клієнт WebSocket поверх socket: текстові кадри, ping/pong, close."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._send_lock = threading.Lock()
        self._buf = b""

    @classmethod
    def connect(cls, host: str, port: int, timeout_s: float = 3.0, path: str = "/") -> "WebSocket":
        sock = socket.create_connection((host, port), timeout=timeout_s)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                         .encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("rosbridge закрив зʼєднання під час рукостискання")
                head += chunk
                if len(head) > 65536:
                    raise ConnectionError("занадто довга відповідь на рукостискання")
            header, rest = head.split(b"\r\n\r\n", 1)
            lines = header.decode("latin-1").split("\r\n")
            if " 101 " not in lines[0] + " ":
                raise ConnectionError(f"rosbridge відповів: {lines[0]}")
            fields = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            if fields.get("sec-websocket-accept") != accept:
                raise ConnectionError("неправильний Sec-WebSocket-Accept")
        except BaseException:
            sock.close()
            raise
        ws = cls(sock)
        ws._buf = rest
        return ws

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return head + mask + masked

    def send_text(self, text: str) -> None:
        data = self._frame(0x1, text.encode())
        with self._send_lock:
            self.sock.sendall(data)

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        with self._send_lock:
            self.sock.sendall(self._frame(opcode, payload))

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("rosbridge закрив зʼєднання")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_text(self) -> str:
        """Наступне текстове повідомлення (кадри-продовження склеюються; ping — відповідь pong)."""
        parts: List[bytes] = []
        while True:
            b0, b1 = self._read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("rosbridge закрив зʼєднання")
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2, 0x0):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8")

    def close(self) -> None:
        try:
            self._send(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# --- зʼєднання з одним Pi -------------------------------------------------------------------------------
class BridgeConnection:
    """Підтримує зʼєднання з rosbridge одного Pi у фоновому потоці й перепідключається після обриву."""

    def __init__(self, host: str, port: int, connect_timeout_s: float = 3.0,
                 retry_s: Tuple[float, float] = (0.5, 3.0), ws_factory=WebSocket.connect) -> None:
        self.host, self.port = host, port
        self.connect_timeout_s = connect_timeout_s
        self.retry_s = retry_s
        self._ws_factory = ws_factory
        self._ws: Optional[WebSocket] = None
        self._lock = threading.Lock()
        self._adverts: Dict[str, str] = {}
        self._subs: Dict[str, Tuple[str, List[Callable[[dict], None]]]] = {}
        self._pending: Dict[str, Tuple[Future, Callable]] = {}
        self._ids = itertools.count(1)
        self._stop = threading.Event()
        self.last_error = ""
        self._thread = threading.Thread(target=self._run, name=f"rosbridge-{host}:{port}", daemon=True)
        self._thread.start()

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def connected(self) -> bool:
        return self._ws is not None

    # реєстрація (діє й після перепідключення)
    def advertise(self, topic: str, msg_type: str) -> None:
        with self._lock:
            self._adverts[topic] = msg_type
        self._send({"op": "advertise", "topic": topic, "type": msg_type})

    def unadvertise(self, topic: str) -> None:
        with self._lock:
            known = self._adverts.pop(topic, None)
        if known:
            self._send({"op": "unadvertise", "topic": topic})

    def subscribe(self, topic: str, msg_type: str, callback: Callable[[dict], None]) -> None:
        with self._lock:
            first = topic not in self._subs
            self._subs.setdefault(topic, (msg_type, []))[1].append(callback)
        if first:
            self._send({"op": "subscribe", "topic": topic, "type": msg_type, "queue_length": 1})

    def resubscribe(self, topic: str) -> None:
        """Підписатися заново: rosbridge підбирає QoS за видавцем, що є в момент підписки. Якщо Polar
        підписався раніше, ніж вузол на Pi почав публікувати (BEST_EFFORT), дані можуть не йти ніколи."""
        with self._lock:
            entry = self._subs.get(topic)
        if entry and self._send({"op": "unsubscribe", "topic": topic}):
            self._send({"op": "subscribe", "topic": topic, "type": entry[0], "queue_length": 1})

    def unsubscribe(self, topic: str) -> None:
        with self._lock:
            known = self._subs.pop(topic, None)
        if known:
            self._send({"op": "unsubscribe", "topic": topic})

    def publish(self, topic: str, msg: dict) -> bool:
        return self._send({"op": "publish", "topic": topic, "msg": msg})

    def call(self, service: str, srv_type: str, args: dict, convert: Callable = lambda v: v) -> Future:
        out: Future = Future()
        if not self.connected:
            out.set_exception(TimeoutError(f"немає звʼязку з rosbridge на Pi {self.address}"
                                           + (f": {self.last_error}" if self.last_error else "")))
            return out
        cid = f"polar-{next(self._ids)}"
        with self._lock:
            self._pending[cid] = (out, convert)
        if not self._send({"op": "call_service", "id": cid, "service": service, "type": srv_type, "args": args}):
            with self._lock:
                self._pending.pop(cid, None)
            if not out.done():
                out.set_exception(TimeoutError(f"немає звʼязку з rosbridge на Pi {self.address}"))
        return out

    def forget(self, cid_future: Future) -> None:
        with self._lock:
            for cid, (f, _c) in list(self._pending.items()):
                if f is cid_future:
                    self._pending.pop(cid)

    def close(self) -> None:
        self._stop.set()
        ws = self._ws
        self._ws = None
        if ws:
            ws.close()
        self._thread.join(timeout=2)
        self._fail_pending("зʼєднання закрито")

    # внутрішнє
    def _send(self, obj: dict) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            ws.send_text(json.dumps(obj))
            return True
        except OSError as e:
            self.last_error = str(e)
            self._drop(ws)
            return False

    def _drop(self, ws: WebSocket) -> None:
        if self._ws is ws:
            self._ws = None
            ws.close()
            self._fail_pending(f"обрив зʼєднання з {self.address}")

    def _fail_pending(self, why: str) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
        for f, _c in pending.values():
            if not f.done():
                f.set_exception(ConnectionError(why))

    def _run(self) -> None:
        delay = self.retry_s[0]
        while not self._stop.is_set():
            try:
                ws = self._ws_factory(self.host, self.port, self.connect_timeout_s)
            except OSError as e:
                self.last_error = str(e) or type(e).__name__
                self._stop.wait(delay)
                delay = min(self.retry_s[1], delay * 2)
                continue
            ws.sock.settimeout(None)
            delay = self.retry_s[0]
            self.last_error = ""
            self._ws = ws
            log.info("rosbridge: підключено %s", self.address)
            with self._lock:
                adverts = list(self._adverts.items())
                subs = [(t, v[0]) for t, v in self._subs.items()]
            for topic, msg_type in adverts:
                self._send({"op": "advertise", "topic": topic, "type": msg_type})
            for topic, msg_type in subs:
                self._send({"op": "subscribe", "topic": topic, "type": msg_type, "queue_length": 1})
            try:
                while not self._stop.is_set():
                    self._dispatch(json.loads(ws.recv_text()))
            except (OSError, ValueError) as e:
                if not self._stop.is_set():
                    self.last_error = str(e) or type(e).__name__
                    log.warning("rosbridge %s: %s", self.address, self.last_error)
            self._drop(ws)
            self._stop.wait(delay)

    def _dispatch(self, m: dict) -> None:
        op = m.get("op")
        if op == "publish":
            with self._lock:
                entry = self._subs.get(m.get("topic"))
                callbacks = list(entry[1]) if entry else []
            for cb in callbacks:
                try:
                    cb(m.get("msg") or {})
                except Exception:  # noqa: BLE001 — помилка обробника не рве зʼєднання
                    log.exception("rosbridge: обробник %s", m.get("topic"))
        elif op == "service_response":
            with self._lock:
                entry = self._pending.pop(m.get("id"), None)
            if entry is None:
                return
            f, convert = entry
            if f.done():
                return
            if m.get("result") is False:
                f.set_exception(RuntimeError(f"{m.get('service')}: {m.get('values')}"))
                return
            try:
                f.set_result(convert(m.get("values") or {}))
            except Exception as e:  # noqa: BLE001
                f.set_exception(e)
        elif op == "status" and m.get("level") == "error":
            log.warning("rosbridge %s: %s", self.address, m.get("msg"))


# --- перетворення повідомлень ---------------------------------------------------------------------------
def settings_to_dict(s: ServoSettings) -> dict:
    return {"id": int(s.servo_id), "direction": int(s.direction), "zero_offset_deg": float(s.zero_offset_deg),
            "gear_ratio": float(s.gear_ratio), "max_speed_deg_s": float(s.max_speed_deg_s),
            "acceleration": int(s.acceleration), "torque_limit_pct": float(s.torque_limit_pct),
            "temperature_limit_c": float(s.temperature_limit_c), "continuous": bool(s.continuous),
            "has_limits": s.min_deg is not None, "min_deg": float(s.min_deg or 0.0),
            "max_deg": float(s.max_deg or 0.0)}


def telemetry_from_dict(t: dict) -> ServoTelemetry:
    g = t.get
    return ServoTelemetry(servo_id=int(g("id", 0)), online=bool(g("online", False)),
                          position_deg=float(g("position_deg", 0.0)), speed_deg_s=float(g("speed_deg_s", 0.0)),
                          load_pct=float(g("load_pct", 0.0)), voltage_v=float(g("voltage_v", 0.0)),
                          current_a=float(g("current_a", 0.0)), temperature_c=float(g("temperature_c", 0.0)),
                          moving=bool(g("moving", False)))


def snapshot_from_dict(m: dict) -> CameraSnapshot:
    return CameraSnapshot(received_at=time.monotonic(), azimuth_deg=float(m.get("azimuth_deg", 0.0)),
                          elevation_deg=float(m.get("elevation_deg", 0.0)), moving=bool(m.get("moving", False)),
                          error_flags=int(m.get("error_flags", 0)),
                          pan=telemetry_from_dict(m.get("pan") or {}), tilt=telemetry_from_dict(m.get("tilt") or {}))


# --- канал керування --------------------------------------------------------------------------------------
class _Link:
    def __init__(self, conn: BridgeConnection, ns: str, name: str, emit) -> None:
        self.conn, self.ns, self.name = conn, ns, name
        self.snapshot: Optional[CameraSnapshot] = None
        self._emit = emit
        for topic, t in (("cmd", T_CMD), ("jog", T_JOG), ("heartbeat", T_EMPTY)):
            conn.advertise(f"{ns}/{topic}", t)
        conn.subscribe(f"{ns}/state", T_STATE, self._on_state)

    RESUBSCRIBE_S = 3.0

    def _on_state(self, msg: dict) -> None:
        self.snapshot = snapshot_from_dict(msg)
        self._emit(self.name, self.snapshot)

    def check_state(self, now: float) -> None:
        """Немає state довше RESUBSCRIBE_S (і з часу останньої спроби) — підписатися заново."""
        if not self.conn.connected:
            self._tried = now
            return
        last = max(self.snapshot.received_at if self.snapshot else 0.0, getattr(self, "_tried", 0.0))
        if now - last > self.RESUBSCRIBE_S:
            self._tried = now
            self.conn.resubscribe(f"{self.ns}/state")

    def detach(self) -> None:
        for topic in ("cmd", "jog", "heartbeat"):
            self.conn.unadvertise(f"{self.ns}/{topic}")
        self.conn.unsubscribe(f"{self.ns}/state")


class RosbridgeControlBackend(ControlBackend):
    def __init__(self, service_timeout_s: float = 5.0, connection_factory=BridgeConnection) -> None:
        self.service_timeout_s = service_timeout_s
        self._factory = connection_factory
        self._conns: Dict[Tuple[str, int], BridgeConnection] = {}
        self._links: Dict[str, _Link] = {}
        self._no_host: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._hb_stop = threading.Event()
        self._hb_period = 0.5
        self._hb_thread: Optional[threading.Thread] = None

    def start(self, cameras: Iterable[CameraControlConfig], ros: RosSettings) -> None:
        cams = list(cameras)
        with self._lock:
            wanted = {}
            self._no_host = {}
            for c in cams:
                if not c.host:
                    self._no_host[c.name] = ("не задано адресу Raspberry Pi — вкажіть її у формі камери "
                                             "(«Адреса Raspberry Pi») або адресу потоку RTSP")
                    continue
                wanted[c.name] = ((c.host, int(ros.bridge_port)), ros.namespace(c.namespace))
            for name in list(self._links):
                link = self._links[name]
                if name not in wanted or (link.conn.host, link.conn.port) != wanted[name][0] \
                        or link.ns != wanted[name][1]:
                    self._links.pop(name).detach()
            for name, (key, ns) in wanted.items():
                if name in self._links:
                    continue
                conn = self._conns.get(key)
                if conn is None:
                    conn = self._conns[key] = self._factory(*key)
                self._links[name] = _Link(conn, ns, name, self._emit)
            used = {(link.conn.host, link.conn.port) for link in self._links.values()}
            for key in [k for k in self._conns if k not in used]:
                self._conns.pop(key).close()
            # сигнал присутності — у 4 рази частіше за тайм-аут, щоб одна втрата не зупиняла камери
            self._hb_period = max(0.05, ros.link_timeout_s / 4)
        if self._hb_thread is None:
            self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="rosbridge-heartbeat",
                                               daemon=True)
            self._hb_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.wait(self._hb_period):
            with self._lock:
                links = list(self._links.values())
            now = time.monotonic()
            for link in links:
                link.conn.publish(f"{link.ns}/heartbeat", {})
                link.check_state(now)

    def shutdown(self) -> None:
        self._hb_stop.set()
        with self._lock:
            conns, self._conns, self._links = list(self._conns.values()), {}, {}
        for c in conns:
            c.close()

    # --- допоміжне ---------------------------------------------------------
    def _link(self, name: str) -> _Link:
        with self._lock:
            link = self._links.get(name)
            why = self._no_host.get(name)
        if link is None:
            raise KeyError(f"{name}: {why}" if why else name)
        return link

    def connection_info(self, name: str) -> str:
        """Для повідомлень оператору: з яким Pi звʼязок і в якому він стані."""
        try:
            link = self._link(name)
        except KeyError as e:
            return str(e.args[0])
        c = link.conn
        return f"rosbridge {c.address}: " + ("підключено" if c.connected else f"немає звʼязку {c.last_error}".strip())

    def _call(self, name: str, service: str, srv_type: str, args: dict, convert,
              timeout_s: Optional[float] = None) -> Future:
        try:
            link = self._link(name)
        except KeyError as e:
            return failed(KeyError(e.args[0]))
        out = link.conn.call(f"{link.ns}/{service}", srv_type, args, convert)
        limit = timeout_s or self.service_timeout_s

        def timeout():
            if not out.done():
                link.conn.forget(out)
                out.set_exception(TimeoutError(f"{name}: Pi не відповів за {limit:g} с"))
        timer = threading.Timer(limit, timeout)
        timer.daemon = True
        timer.start()
        return out

    def _publish(self, name: str, topic: str, msg: dict) -> None:
        link = self._link(name)
        if not link.conn.publish(f"{link.ns}/{topic}", msg):
            raise ConnectionError(f"{name}: немає звʼязку з Pi ({self.connection_info(name)})")

    # --- ControlBackend -----------------------------------------------------
    def command(self, name, azimuth_deg, elevation_deg, speed_deg_s) -> None:
        self._publish(name, "cmd", {"azimuth_deg": float(azimuth_deg), "elevation_deg": float(elevation_deg),
                                    "speed_deg_s": float(speed_deg_s)})

    def jog(self, name, d_azimuth_deg, d_elevation_deg, speed_deg_s) -> None:
        self._publish(name, "jog", {"d_azimuth_deg": float(d_azimuth_deg),
                                    "d_elevation_deg": float(d_elevation_deg), "speed_deg_s": float(speed_deg_s)})

    def stop(self, name) -> Future:
        return self._call(name, "stop", S_TRIGGER, {},
                          lambda v: (bool(v.get("success")), str(v.get("message", ""))))

    def configure(self, cfg: CameraControlConfig, ros: RosSettings) -> Future:
        args = {"port": cfg.port, "baudrate": int(cfg.baudrate), "pan": settings_to_dict(cfg.pan),
                "tilt": settings_to_dict(cfg.tilt), "state_rate_hz": float(ros.state_rate_hz),
                "link_timeout_s": float(ros.link_timeout_s)}
        return self._call(cfg.name, "configure", S_CONFIGURE, args,
                          lambda v: (bool(v.get("success")), str(v.get("message", ""))))

    def scan(self, name, first_id=1, last_id=253) -> Future:
        return self._call(name, "scan", S_SCAN, {"first_id": int(first_id), "last_id": int(last_id)},
                          lambda v: _ids(v.get("ids")), timeout_s=20.0)   # 253 ID × тайм-аут PING

    def set_zero(self, name, axis) -> Future:
        def convert(v):
            if not v.get("success"):
                raise RuntimeError(v.get("message", ""))
            return float(v.get("zero_offset_deg", 0.0))
        return self._call(name, "set_zero", S_SET_ZERO, {"axis": axis}, convert)

    def set_servo_id(self, name, old_id, new_id) -> Future:
        return self._call(name, "set_servo_id", S_SET_ID, {"old_id": int(old_id), "new_id": int(new_id)},
                          lambda v: (bool(v.get("success")), str(v.get("message", ""))))

    def snapshot(self, name) -> Optional[CameraSnapshot]:
        with self._lock:
            link = self._links.get(name)
        return link.snapshot if link else None


def _ids(raw) -> List[int]:
    """uint8[] у rosbridge приходить або списком, або рядком base64 (як bytes)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return list(base64.b64decode(raw))
    return [int(x) for x in raw]


__all__ = ["RosbridgeControlBackend", "BridgeConnection", "WebSocket"]
