"""Вузол ROS 2 pantilt_driver: керує двома сервоприводами ST3020 однієї камери."""
from __future__ import annotations

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from std_msgs.msg import Empty
from std_srvs.srv import Trigger

from polar_interfaces.msg import (PanTiltCommand, PanTiltJog, PanTiltState, ServoSettings,
                                  ServoTelemetry)
from polar_interfaces.srv import ConfigureServos, ScanServos, SetServoId, SetZero

from .backend import SimBackend
from .controller import PanTiltController
from .servo import AxisStatus, ServoConfig


def _servo_config(node: Node, prefix: str, defaults: dict) -> ServoConfig:
    def p(name, value):
        return node.declare_parameter(f"{prefix}.{name}", value).value

    min_deg = p("min_deg", defaults.get("min_deg", -1.0))
    max_deg = p("max_deg", defaults.get("max_deg", -1.0))
    return ServoConfig(
        servo_id=p("id", defaults["id"]),
        direction=p("direction", 1),
        zero_offset_deg=p("zero_offset_deg", 0.0),
        gear_ratio=p("gear_ratio", 1.0),
        max_speed_deg_s=p("max_speed_deg_s", 180.0),
        acceleration=p("acceleration", 50),
        torque_limit_pct=p("torque_limit_pct", 100.0),
        temperature_limit_c=p("temperature_limit_c", 70.0),
        min_deg=None if min_deg < 0 else min_deg,  # -1 = без меж
        max_deg=None if max_deg < 0 else max_deg,
        continuous=p("continuous", defaults.get("continuous", False)),
    )


def settings_to_config(msg: ServoSettings) -> ServoConfig:
    return ServoConfig(
        servo_id=msg.id,
        direction=msg.direction,
        zero_offset_deg=msg.zero_offset_deg,
        gear_ratio=msg.gear_ratio,
        max_speed_deg_s=msg.max_speed_deg_s,
        acceleration=msg.acceleration,
        torque_limit_pct=msg.torque_limit_pct,
        temperature_limit_c=msg.temperature_limit_c,
        min_deg=msg.min_deg if msg.has_limits else None,
        max_deg=msg.max_deg if msg.has_limits else None,
        continuous=msg.continuous,
    )


class PanTiltNode(Node):
    def __init__(self) -> None:
        super().__init__("pantilt_driver")
        backend_name = self.declare_parameter("backend", "sim").value
        rate_hz = self.declare_parameter("state_rate_hz", 20.0).value
        link_timeout_s = self.declare_parameter("link_timeout_s", 2.0).value

        pan = _servo_config(self, "pan", {"id": 1, "continuous": True})
        tilt = _servo_config(self, "tilt", {"id": 2, "min_deg": 0.0, "max_deg": 90.0})

        self.backend_name = backend_name
        self.port = self.declare_parameter("port", "/dev/serial0").value
        self.baudrate = self.declare_parameter("baudrate", 1_000_000).value
        backend = self._open_backend(backend_name, [pan.servo_id, tilt.servo_id])
        self.ctrl = PanTiltController(backend, pan, tilt, link_timeout_s=link_timeout_s)

        cmd_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                             history=HistoryPolicy.KEEP_LAST)
        state_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                               history=HistoryPolicy.KEEP_LAST)
        self.create_subscription(PanTiltCommand, "cmd", self._on_cmd, cmd_qos)
        self.create_subscription(PanTiltJog, "jog", self._on_jog, cmd_qos)
        self.create_subscription(Empty, "heartbeat", lambda _m: self.ctrl.heartbeat(), cmd_qos)
        self.create_service(Trigger, "stop", self._on_stop)
        self.create_service(ConfigureServos, "configure", self._on_configure)
        self.create_service(ScanServos, "scan", self._on_scan)
        self.create_service(SetZero, "set_zero", self._on_set_zero)
        self.create_service(SetServoId, "set_servo_id", self._on_set_servo_id)
        self.state_pub = self.create_publisher(PanTiltState, "state", state_qos)
        self.rate_hz = rate_hz
        self.timer = self.create_timer(1.0 / rate_hz, self._tick)
        self.get_logger().info(
            f"pantilt_driver: backend={backend_name}, pan id={pan.servo_id}, tilt id={tilt.servo_id}")

    def _open_backend(self, name: str, servo_ids):
        if name == "sim":
            return SimBackend(servo_ids)
        if name == "st3020":
            from .st3020 import ST3020Bus

            return ST3020Bus(self.port, self.baudrate)
        raise ValueError(f"Невідомий backend: {name} (sim | st3020)")

    def _on_configure(self, req, resp):
        try:
            pan, tilt = settings_to_config(req.pan), settings_to_config(req.tilt)
            if self.backend_name == "st3020" and req.port and (
                    req.port != self.port or req.baudrate != self.baudrate):
                self.ctrl.stop()
                self.ctrl.backend.close()
                self.port, self.baudrate = req.port, int(req.baudrate)
                self.ctrl.backend = self._open_backend("st3020", [])
            self.ctrl.reconfigure(pan, tilt, req.link_timeout_s if req.link_timeout_s > 0 else None)
            if req.state_rate_hz > 0 and abs(req.state_rate_hz - self.rate_hz) > 1e-9:
                self.timer.cancel()
                self.rate_hz = req.state_rate_hz
                self.timer = self.create_timer(1.0 / self.rate_hz, self._tick)
            resp.success, resp.message = True, "налаштування застосовано"
        except (ValueError, OSError) as e:
            resp.success, resp.message = False, str(e)
        self.get_logger().info(f"configure: {resp.message}")
        return resp

    def _on_scan(self, req, resp):
        first, last = max(1, req.first_id or 1), min(253, req.last_id or 253)
        resp.ids = self.ctrl.backend.scan(range(first, last + 1))
        return resp

    def _on_set_zero(self, req, resp):
        try:
            resp.zero_offset_deg = self.ctrl.zero_here(req.axis)
            resp.success, resp.message = True, "нуль встановлено"
        except (ValueError, OSError) as e:
            resp.success, resp.message = False, str(e)
        return resp

    def _on_set_servo_id(self, req, resp):
        try:
            self.ctrl.backend.set_id(req.old_id, req.new_id)
            resp.success, resp.message = True, f"ID {req.old_id} → {req.new_id}"
        except (ValueError, KeyError, OSError, NotImplementedError) as e:
            resp.success, resp.message = False, str(e)
        return resp

    def _on_cmd(self, msg: PanTiltCommand) -> None:
        self.ctrl.command(msg.azimuth_deg, msg.elevation_deg, msg.speed_deg_s)

    def _on_jog(self, msg: PanTiltJog) -> None:
        self.ctrl.jog(msg.d_azimuth_deg, msg.d_elevation_deg, msg.speed_deg_s)

    def _on_stop(self, _req, resp):
        self.ctrl.stop()
        resp.success = True
        resp.message = "stopped"
        return resp

    @staticmethod
    def _telemetry(servo_id: int, status: AxisStatus) -> ServoTelemetry:
        t = ServoTelemetry(id=servo_id, online=status.online, position_deg=status.axis_deg)
        r = status.reading
        if r is not None:
            t.speed_deg_s = r.speed_deg_s
            t.load_pct = r.load_pct
            t.voltage_v = r.voltage_v
            t.current_a = r.current_a
            t.temperature_c = r.temperature_c
            t.moving = r.moving
        return t

    def _tick(self) -> None:
        st = self.ctrl.update()
        msg = PanTiltState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.get_namespace().strip("/")
        msg.azimuth_deg = st.azimuth_deg
        msg.elevation_deg = st.elevation_deg
        msg.moving = st.moving
        msg.error_flags = st.error_flags
        msg.pan = self._telemetry(self.ctrl.pan.servo_id, st.pan)
        msg.tilt = self._telemetry(self.ctrl.tilt.servo_id, st.tilt)
        self.state_pub.publish(msg)

    def destroy_node(self):
        self.ctrl.stop()
        self.ctrl.backend.close()
        return super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = PanTiltNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
