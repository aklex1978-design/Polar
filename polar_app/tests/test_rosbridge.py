"""Канал rosbridge: власний WebSocket-клієнт і RosbridgeControlBackend проти імітатора rosbridge_server.

Імітатор — справжній сервер WebSocket на стандартній бібліотеці (рукостискання, маскування кадрів,
фрагментація) з поведінкою вузла pantilt_driver: cmd → кути, state 20 Гц, сервіси stop/scan/configure.
"""
import base64
import hashlib
import json
import socket
import socketserver
import struct
import threading
import time

import pytest

from polar_app.control import rosbridge as rb
from polar_app.control.mixed import MixedControlBackend, _default_ros
from polar_core import control as ctl
from polar_core import db
from polar_core.cameras import CameraRecord


def wait(seconds, until):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until():
            return True
        time.sleep(0.02)
    return False


# --- імітатор rosbridge_server -------------------------------------------------------------------------
def _send_frame(sock, opcode, payload, fragment=False):
    if fragment and len(payload) > 4:                    # текст двома кадрами: текст + продовження
        half = len(payload) // 2
        _send_frame_raw(sock, opcode, payload[:half], fin=False)
        _send_frame_raw(sock, 0x0, payload[half:], fin=True)
        return
    _send_frame_raw(sock, opcode, payload, fin=True)


def _send_frame_raw(sock, opcode, payload, fin):
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    sock.sendall(head + payload)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


def _recv_frame(sock):
    b0, b1 = _recv_exact(sock, 2)
    assert b1 & 0x80, "кадри клієнта мають бути замасковані"
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(_recv_exact(sock, n)))
    return b0 & 0x0F, data


class FakePi:
    """Стан «вузла pantilt_driver» за rosbridge."""

    def __init__(self):
        self.az, self.el = 0.0, 0.0
        self.heartbeats = 0
        self.ops = []                     # усі отримані повідомлення
        self.reply_services = True
        self.ids_as_base64 = True
        self.big_state = False            # додати в state довге поле (кадр > 65535 байт)
        self.fragment = False
        self.ignore_subscribes = 0        # скільки перших підписок на state «не спрацюють» (QoS не збігся)
        self.lock = threading.Lock()
        self.clients = []


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        pi: FakePi = self.server.pi
        sock = self.request
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(4096)
        key = [ln.split(":", 1)[1].strip() for ln in head.decode().split("\r\n")
               if ln.lower().startswith("sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1((key + rb.WS_GUID).encode()).digest()).decode()
        sock.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        with pi.lock:
            pi.clients.append(sock)
        send_lock = threading.Lock()
        subscribed = threading.Event()
        alive = threading.Event()
        alive.set()

        def send(obj):
            with send_lock:
                _send_frame(sock, 0x1, json.dumps(obj).encode(), fragment=pi.fragment)

        def publisher():
            while alive.is_set():
                if subscribed.is_set():
                    msg = {"header": {"stamp": {"sec": 1, "nanosec": 2}, "frame_id": "x"},
                           "azimuth_deg": pi.az, "elevation_deg": pi.el, "moving": False, "error_flags": 0,
                           "pan": {"id": 1, "online": True, "position_deg": pi.az, "voltage_v": 12.1,
                                   "temperature_c": 23.0},
                           "tilt": {"id": 2, "online": True, "position_deg": pi.el, "voltage_v": 12.0}}
                    if pi.big_state:
                        msg["padding"] = "x" * 70000
                    try:
                        send({"op": "publish", "topic": "/polar/camera_1/state", "msg": msg})
                    except OSError:
                        return
                time.sleep(0.05)

        threading.Thread(target=publisher, daemon=True).start()
        try:
            while True:
                opcode, data = _recv_frame(sock)
                if opcode == 0x8:
                    return
                if opcode == 0x9:
                    with send_lock:
                        _send_frame(sock, 0xA, data)
                    continue
                if opcode == 0xA:
                    with pi.lock:
                        pi.pongs = getattr(pi, "pongs", 0) + 1
                    continue
                m = json.loads(data)
                with pi.lock:
                    pi.ops.append(m)
                op, topic = m.get("op"), m.get("topic", "")
                if op == "subscribe" and topic.endswith("/state"):
                    assert m["type"] == rb.T_STATE
                    if pi.ignore_subscribes > 0:
                        pi.ignore_subscribes -= 1
                        continue
                    subscribed.set()
                    with send_lock:                                   # rosbridge іноді шле ping
                        _send_frame(sock, 0x9, b"hi")
                elif op == "publish" and topic.endswith("/cmd"):
                    pi.az, pi.el = m["msg"]["azimuth_deg"], m["msg"]["elevation_deg"]
                elif op == "publish" and topic.endswith("/jog"):
                    pi.az += m["msg"]["d_azimuth_deg"]
                    pi.el += m["msg"]["d_elevation_deg"]
                elif op == "publish" and topic.endswith("/heartbeat"):
                    pi.heartbeats += 1
                elif op == "call_service" and pi.reply_services:
                    srv = m["service"].rsplit("/", 1)[1]
                    if srv == "scan":
                        ids = bytes([1, 2])
                        values = {"ids": base64.b64encode(ids).decode() if pi.ids_as_base64 else [1, 2]}
                    elif srv == "set_zero":
                        values = {"success": True, "message": "", "zero_offset_deg": 12.5}
                    elif srv == "configure":
                        values = {"success": True, "message": f"port {m['args']['port']}"}
                    elif srv == "set_servo_id":
                        values = {"success": False, "message": "ID 2 вже зайнятий"}
                    else:
                        values = {"success": True, "message": "stopped"}
                    send({"op": "service_response", "id": m["id"], "service": m["service"],
                          "values": values, "result": True})
        except (ConnectionError, OSError):
            pass
        finally:
            alive.clear()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture
def pi():
    srv = Server(("127.0.0.1", 0), Handler)
    srv.pi = FakePi()
    srv.pi.port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.pi
    srv.shutdown()
    srv.server_close()
    for s in srv.pi.clients:
        try:
            s.close()
        except OSError:
            pass


def ros_settings(port):
    return ctl.RosSettings(transport="rosbridge", bridge_port=port, link_timeout_s=0.4)


def cam(host="127.0.0.1", name="Камера 1", ns="camera_1"):
    return ctl.CameraControlConfig(camera_id=1, name=name, namespace=ns, host=host,
                                   port="/dev/serial/by-id/usb-1a86", baudrate=1_000_000)


@pytest.fixture
def backend(pi):
    b = rb.RosbridgeControlBackend(service_timeout_s=1.0,
                                   connection_factory=lambda h, p: rb.BridgeConnection(h, p, retry_s=(0.05, 0.2)))
    states = []
    b.add_state_listener(lambda name, snap: states.append((name, snap)))
    b.states = states
    b.start([cam()], ros_settings(pi.port))
    yield b
    b.shutdown()


# --- тести ---------------------------------------------------------------------------------------------
def test_state_command_jog_and_heartbeat(pi, backend):
    assert wait(5, lambda: backend.snapshot("Камера 1") is not None)
    snap = backend.snapshot("Камера 1")
    assert snap.pan.online and snap.pan.voltage_v == pytest.approx(12.1) and snap.tilt.servo_id == 2
    assert backend.states and backend.states[-1][0] == "Камера 1"
    backend.command("Камера 1", 20.0, 30.0, 20.0)
    assert wait(3, lambda: backend.snapshot("Камера 1").azimuth_deg == 20.0
                and backend.snapshot("Камера 1").elevation_deg == 30.0)
    backend.jog("Камера 1", -5.0, 1.0, 10.0)
    assert wait(3, lambda: backend.snapshot("Камера 1").azimuth_deg == 15.0)
    assert getattr(pi, "pongs", 0) >= 1                              # на ping rosbridge — pong
    hb = pi.heartbeats
    assert wait(2, lambda: pi.heartbeats >= hb + 5)                  # тайм-аут 0,4 с → кожні 0,1 с
    adv = {m["topic"]: m["type"] for m in pi.ops if m["op"] == "advertise"}
    assert adv == {"/polar/camera_1/cmd": rb.T_CMD, "/polar/camera_1/jog": rb.T_JOG,
                   "/polar/camera_1/heartbeat": rb.T_EMPTY}


def test_services(pi, backend):
    assert wait(5, lambda: backend.snapshot("Камера 1") is not None)
    assert backend.stop("Камера 1").result(3) == (True, "stopped")
    assert backend.scan("Камера 1", 1, 10).result(3) == [1, 2]                   # uint8[] як base64
    pi.ids_as_base64 = False
    assert backend.scan("Камера 1").result(3) == [1, 2]                          # або списком
    ok, msg = backend.configure(cam(), ros_settings(pi.port)).result(3)
    assert ok and msg == "port /dev/serial/by-id/usb-1a86"
    call = [m for m in pi.ops if m.get("op") == "call_service" and m["service"].endswith("configure")][0]
    assert call["type"] == rb.S_CONFIGURE and call["args"]["pan"]["id"] == 1 and call["args"]["tilt"]["id"] == 2
    assert call["args"]["link_timeout_s"] == pytest.approx(0.4)
    assert backend.set_zero("Камера 1", "pan").result(3) == pytest.approx(12.5)
    assert backend.set_zero("Камера 1", "tilt", recenter=True).result(3) == pytest.approx(12.5)
    zs = [m["args"] for m in pi.ops if m.get("op") == "call_service" and m["service"].endswith("set_zero")]
    assert zs == [{"axis": "pan"}, {"axis": "tilt", "recenter": True}]     # старим Pi поле не надсилається
    assert backend.set_servo_id("Камера 1", 1, 2).result(3) == (False, "ID 2 вже зайнятий")


def test_service_timeout(pi, backend):
    assert wait(5, lambda: backend.snapshot("Камера 1") is not None)
    pi.reply_services = False
    with pytest.raises(TimeoutError, match="не відповів"):
        backend.stop("Камера 1").result(3)


def test_reconnect_resubscribes(pi, backend):
    assert wait(5, lambda: backend.snapshot("Камера 1") is not None)
    for s in list(pi.clients):                                    # обрив мережі
        s.shutdown(socket.SHUT_RDWR)
        s.close()
    pi.clients.clear()
    n_sub = sum(1 for m in pi.ops if m["op"] == "subscribe")
    assert wait(5, lambda: sum(1 for m in pi.ops if m["op"] == "subscribe") > n_sub)
    t0 = time.monotonic()
    assert wait(5, lambda: backend.snapshot("Камера 1").received_at > t0)       # state знову йде
    backend.command("Камера 1", 45.0, 10.0, 30.0)                                # cmd оголошено заново
    assert wait(3, lambda: pi.az == 45.0)
    assert sum(1 for m in pi.ops if m["op"] == "advertise" and m["topic"].endswith("/cmd")) >= 2


def test_large_and_fragmented_messages(pi, backend):
    pi.big_state, pi.fragment = True, True
    assert wait(5, lambda: backend.snapshot("Камера 1") is not None)
    t0 = time.monotonic()
    assert wait(5, lambda: backend.snapshot("Камера 1").received_at > t0)


def test_no_pi_and_no_host():
    b = rb.RosbridgeControlBackend(service_timeout_s=0.5,
                                   connection_factory=lambda h, p: rb.BridgeConnection(h, p, connect_timeout_s=0.3,
                                                                                       retry_s=(0.05, 0.1)))
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()                                               # на цьому порту нікого немає
    b.start([cam(), cam(host="", name="Без адреси", ns="camera_2")], ros_settings(port))
    try:
        with pytest.raises(TimeoutError, match="немає звʼязку з rosbridge"):
            b.stop("Камера 1").result(2)
        with pytest.raises(ConnectionError, match="немає звʼязку"):
            b.command("Камера 1", 1, 1, 1)
        assert "немає звʼязку" in b.connection_info("Камера 1")
        with pytest.raises(KeyError, match="адресу Raspberry Pi"):
            b.stop("Без адреси").result(1)
        assert "адресу Raspberry Pi" in b.connection_info("Без адреси")
    finally:
        b.shutdown()


def test_restart_changes_host_and_shares_connection(pi):
    made = []

    def factory(h, p):
        made.append((h, p))
        return rb.BridgeConnection(h, p, retry_s=(0.05, 0.2))
    b = rb.RosbridgeControlBackend(connection_factory=factory)
    try:
        # дві камери на одному Pi (на практиці — різні Pi) — одне зʼєднання
        b.start([cam(), cam(name="Камера 2", ns="camera_2")], ros_settings(pi.port))
        assert made == [("127.0.0.1", pi.port)]
        assert wait(5, lambda: b.snapshot("Камера 1") is not None)
        b.start([cam(name="Камера 2", ns="camera_2")], ros_settings(pi.port))    # камеру 1 видалено
        assert wait(3, lambda: any(m["op"] == "unsubscribe" and m["topic"] == "/polar/camera_1/state"
                                   for m in pi.ops))
        assert b.snapshot("Камера 1") is None and len(made) == 1
    finally:
        b.shutdown()


def test_resubscribes_when_state_never_arrives(pi, monkeypatch):
    """rosbridge підписався до появи видавця (QoS не збігся) — state не йде; Polar підписується заново."""
    monkeypatch.setattr(rb._Link, "RESUBSCRIBE_S", 0.5)
    pi.ignore_subscribes = 1
    b = rb.RosbridgeControlBackend(connection_factory=lambda h, p: rb.BridgeConnection(h, p, retry_s=(0.05, 0.2)))
    try:
        b.start([cam()], ros_settings(pi.port))
        assert wait(5, lambda: b.snapshot("Камера 1") is not None)
        ops = [m["op"] for m in pi.ops if m.get("topic") == "/polar/camera_1/state"]
        assert ops[:3] == ["subscribe", "unsubscribe", "subscribe"]
    finally:
        b.shutdown()


def test_snapshot_from_partial_message():
    s = rb.snapshot_from_dict({"azimuth_deg": 1.5, "pan": {"id": 1, "online": True}})
    assert s.azimuth_deg == 1.5 and s.pan.online and not s.tilt.online and s.error_flags == 0


# --- налаштування: адреса Pi, вибір каналу ----------------------------------------------------------------
def test_host_from_stream_url_and_pi_host(tmp_path):
    assert ctl.host_from_url("rtsp://admin@192.168.0.120:8554/camera_1") == "192.168.0.120"
    assert ctl.host_from_url("/dev/video0") == "" and ctl.host_from_url("0") == ""
    conn = db.connect(tmp_path / "p.sqlite")
    with conn:
        conn.execute("INSERT INTO cameras (name, resolution_x, resolution_y, fov_x_deg, fov_y_deg, ros_namespace, "
                     "stream_url) VALUES ('А', 720, 480, 24, 18, 'camera_1', 'rtsp://192.168.0.120:8554/camera_1')")
        conn.execute("INSERT INTO cameras (name, resolution_x, resolution_y, fov_x_deg, fov_y_deg, ros_namespace, "
                     "stream_url, pi_host) VALUES ('Б', 720, 480, 24, 18, 'camera_2', 'rtsp://10.0.0.5/x', "
                     "'polar-camera-2.local')")
    hosts = {c.name: c.host for c in ctl.load_control_configs(conn)}
    assert hosts == {"А": "192.168.0.120", "Б": "polar-camera-2.local"}
    assert "pi_host" in CameraRecord(pi_host="bad host!").validate([])
    assert "pi_host" not in CameraRecord(pi_host="192.168.0.120").validate([])


def test_transport_setting_and_default_backend(tmp_path):
    conn = db.connect(tmp_path / "p.sqlite")
    s = ctl.load_ros_settings(conn)
    assert s.transport == "rosbridge" and s.bridge_port == 9090                  # за замовчуванням
    s.bridge_port = 9091
    ctl.save_ros_settings(conn, s)
    assert ctl.load_ros_settings(conn).bridge_port == 9091
    assert "невідомий спосіб" in "; ".join(ctl.RosSettings(transport="x").validate())
    b = _default_ros(s)
    assert isinstance(b, rb.RosbridgeControlBackend)
    b.shutdown()
    m = MixedControlBackend()
    try:
        m.start([cam(host="127.0.0.1")], ctl.RosSettings(bridge_port=1))
        assert isinstance(m.ros, rb.RosbridgeControlBackend) and not m.ros_error
    finally:
        m.shutdown()
