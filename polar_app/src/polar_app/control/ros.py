"""Канал до камер через ROS 2: вузол polar_control на сервері.

Для кожної камери в просторі імен <префікс>/<камера>:
публікує cmd, jog, heartbeat; підписаний на state; викликає сервіси stop, configure, scan,
set_zero, set_servo_id вузла pantilt_driver на Raspberry Pi.

Змінні оточення ROS (ROS_DOMAIN_ID, RMW_IMPLEMENTATION …) задаються до виклику start()
— див. RosSettings.env() і polar_app.__main__.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from typing import Dict, Iterable, Optional

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Empty
from std_srvs.srv import Trigger

from polar_core.control import (CameraControlConfig, CameraSnapshot, RosSettings, ServoSettings,
                                ServoTelemetry)
from polar_interfaces.msg import PanTiltCommand, PanTiltJog, PanTiltState
from polar_interfaces.msg import ServoSettings as ServoSettingsMsg
from polar_interfaces.srv import ConfigureServos, ScanServos, SetServoId, SetZero

from .base import ControlBackend, failed

CMD_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST)
STATE_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST)


def settings_to_msg(s: ServoSettings) -> ServoSettingsMsg:
    m = ServoSettingsMsg()
    m.id, m.direction = s.servo_id, s.direction
    m.zero_offset_deg, m.gear_ratio = float(s.zero_offset_deg), float(s.gear_ratio)
    m.max_speed_deg_s, m.acceleration = float(s.max_speed_deg_s), int(s.acceleration)
    m.torque_limit_pct, m.temperature_limit_c = float(s.torque_limit_pct), float(s.temperature_limit_c)
    m.continuous = s.continuous
    m.has_limits = s.min_deg is not None
    m.min_deg = float(s.min_deg or 0.0)
    m.max_deg = float(s.max_deg or 0.0)
    return m


def telemetry_from_msg(t) -> ServoTelemetry:
    return ServoTelemetry(servo_id=t.id, online=t.online, position_deg=t.position_deg,
                          speed_deg_s=t.speed_deg_s, load_pct=t.load_pct, voltage_v=t.voltage_v,
                          current_a=t.current_a, temperature_c=t.temperature_c, moving=t.moving)


class _CameraLink:
    def __init__(self, node: Node, ns: str, name: str, emit) -> None:
        self.ns = ns
        self.name = name
        self._emit = emit
        self.cmd = node.create_publisher(PanTiltCommand, f"{ns}/cmd", CMD_QOS)
        self.jog = node.create_publisher(PanTiltJog, f"{ns}/jog", CMD_QOS)
        self.heartbeat = node.create_publisher(Empty, f"{ns}/heartbeat", CMD_QOS)
        self.stop = node.create_client(Trigger, f"{ns}/stop")
        self.configure = node.create_client(ConfigureServos, f"{ns}/configure")
        self.scan = node.create_client(ScanServos, f"{ns}/scan")
        self.set_zero = node.create_client(SetZero, f"{ns}/set_zero")
        self.set_servo_id = node.create_client(SetServoId, f"{ns}/set_servo_id")
        self.snapshot: Optional[CameraSnapshot] = None
        self.sub = node.create_subscription(PanTiltState, f"{ns}/state", self._on_state, STATE_QOS)
        self._entities = [self.cmd, self.jog, self.heartbeat]
        self._clients = [self.stop, self.configure, self.scan, self.set_zero, self.set_servo_id]

    def _on_state(self, msg: PanTiltState) -> None:
        self.snapshot = CameraSnapshot(
            received_at=time.monotonic(), azimuth_deg=msg.azimuth_deg, elevation_deg=msg.elevation_deg,
            moving=msg.moving, error_flags=msg.error_flags,
            pan=telemetry_from_msg(msg.pan), tilt=telemetry_from_msg(msg.tilt))
        self._emit(self.name, self.snapshot)

    def destroy(self, node: Node) -> None:
        for p in self._entities:
            node.destroy_publisher(p)
        for c in self._clients:
            node.destroy_client(c)
        node.destroy_subscription(self.sub)


class RosControlBackend(ControlBackend):
    def __init__(self, service_timeout_s: float = 5.0) -> None:
        self.service_timeout_s = service_timeout_s
        self._node: Optional[Node] = None
        self._executor: Optional[MultiThreadedExecutor] = None
        self._thread: Optional[threading.Thread] = None
        self._links: Dict[str, _CameraLink] = {}
        self._lock = threading.Lock()
        self._hb_timer = None

    def start(self, cameras: Iterable[CameraControlConfig], ros: RosSettings) -> None:
        if self._node is None:
            if not rclpy.ok():
                rclpy.init()
            self._node = rclpy.create_node("polar_control")
            self._executor = MultiThreadedExecutor(num_threads=2)
            self._executor.add_node(self._node)
            self._thread = threading.Thread(target=self._executor.spin, name="ros-spin", daemon=True)
            self._thread.start()
        with self._lock:
            wanted = {c.name: ros.namespace(c.namespace) for c in cameras}
            for name in list(self._links):
                if name not in wanted or self._links[name].ns != wanted[name]:
                    self._links.pop(name).destroy(self._node)
            for name, ns in wanted.items():
                if name not in self._links:
                    self._links[name] = _CameraLink(self._node, ns, name, self._emit)
            if self._hb_timer is not None:
                self._node.destroy_timer(self._hb_timer)
            # сигнал присутності — у 4 рази частіше за тайм-аут, щоб одна втрата не зупиняла камери
            self._hb_timer = self._node.create_timer(max(0.05, ros.link_timeout_s / 4), self._heartbeat)

    def _heartbeat(self) -> None:
        with self._lock:
            links = list(self._links.values())
        for link in links:
            link.heartbeat.publish(Empty())

    def shutdown(self) -> None:
        if self._executor:
            self._executor.shutdown()
        if self._node:
            self._node.destroy_node()
            self._node = None
        if rclpy.ok():
            rclpy.try_shutdown()

    # --- допоміжне ---------------------------------------------------------
    def _link(self, name: str) -> _CameraLink:
        with self._lock:
            return self._links[name]

    def _call(self, name: str, client_attr: str, request, convert,
              timeout_s: Optional[float] = None) -> Future:
        try:
            client = getattr(self._link(name), client_attr)
        except KeyError as e:
            return failed(e)
        out: Future = Future()
        if not client.service_is_ready():
            return failed(TimeoutError(f"{name}: сервіс {client.srv_name} недоступний — немає звʼязку з Pi"))
        rf = client.call_async(request)

        def done(f):
            if out.done():
                return
            exc = f.exception()
            if exc is not None:
                out.set_exception(exc)
            else:
                try:
                    out.set_result(convert(f.result()))
                except Exception as e:  # noqa: BLE001
                    out.set_exception(e)
        rf.add_done_callback(done)

        limit = timeout_s or self.service_timeout_s

        def timeout():
            if not out.done():
                out.set_exception(TimeoutError(f"{name}: Pi не відповів за {limit:g} с"))
        timer = threading.Timer(limit, timeout)
        timer.daemon = True
        timer.start()
        return out

    # --- ControlBackend -----------------------------------------------------
    def command(self, name, azimuth_deg, elevation_deg, speed_deg_s) -> None:
        self._link(name).cmd.publish(PanTiltCommand(
            azimuth_deg=float(azimuth_deg), elevation_deg=float(elevation_deg), speed_deg_s=float(speed_deg_s)))

    def jog(self, name, d_azimuth_deg, d_elevation_deg, speed_deg_s) -> None:
        self._link(name).jog.publish(PanTiltJog(
            d_azimuth_deg=float(d_azimuth_deg), d_elevation_deg=float(d_elevation_deg),
            speed_deg_s=float(speed_deg_s)))

    def stop(self, name) -> Future:
        return self._call(name, "stop", Trigger.Request(), lambda r: (r.success, r.message))

    def configure(self, cfg: CameraControlConfig, ros: RosSettings) -> Future:
        req = ConfigureServos.Request(
            port=cfg.port, baudrate=int(cfg.baudrate),
            pan=settings_to_msg(cfg.pan), tilt=settings_to_msg(cfg.tilt),
            state_rate_hz=float(ros.state_rate_hz), link_timeout_s=float(ros.link_timeout_s))
        return self._call(cfg.name, "configure", req, lambda r: (r.success, r.message))

    def scan(self, name, first_id=1, last_id=253) -> Future:
        return self._call(name, "scan", ScanServos.Request(first_id=first_id, last_id=last_id),
                          lambda r: list(r.ids), timeout_s=20.0)  # 253 ID × тайм-аут PING

    def set_zero(self, name, axis) -> Future:
        def convert(r):
            if not r.success:
                raise RuntimeError(r.message)
            return r.zero_offset_deg
        return self._call(name, "set_zero", SetZero.Request(axis=axis), convert)

    def set_servo_id(self, name, old_id, new_id) -> Future:
        return self._call(name, "set_servo_id", SetServoId.Request(old_id=old_id, new_id=new_id),
                          lambda r: (r.success, r.message))

    def snapshot(self, name) -> Optional[CameraSnapshot]:
        with self._lock:
            link = self._links.get(name)
        return link.snapshot if link else None
