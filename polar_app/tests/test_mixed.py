import pytest

from polar_app.control.mixed import MixedControlBackend
from polar_app.control.service import ControlService
from polar_core import cameras as cm

from conftest import Clock


def test_demo_and_real_cameras_side_by_side(conn):
    with conn:
        conn.execute("UPDATE cameras SET demo_servos = 1 WHERE name = 'Камера 1'")
    started = []

    class FakeRos:
        def add_state_listener(self, fn):
            pass

        def start(self, cams, ros):
            started.append([c.name for c in cams])

        def snapshot(self, name):
            return None

        def shutdown(self):
            pass

    b = MixedControlBackend(ros_factory=FakeRos)
    svc = ControlService(conn, b)
    svc.start()
    assert started == [["Камера 2", "Камера 3"]]           # у ROS 2 — лише не демо
    assert b.is_demo("Камера 1") and not b.is_demo("Камера 2")
    import time
    end = time.monotonic() + 3
    while not svc.status("Камера 1").link_ok and time.monotonic() < end:
        time.sleep(0.05)
    assert svc.status("Камера 1").link_ok
    svc.shutdown()


def test_without_ros_only_demo_cameras_work(conn):
    with conn:
        conn.execute("UPDATE cameras SET demo_servos = 1 WHERE name = 'Камера 1'")

    def no_ros():
        raise ImportError("No module named 'rclpy'")
    b = MixedControlBackend(ros_factory=no_ros)
    svc = ControlService(conn, b)
    svc.start()
    assert "rclpy" in b.ros_error
    with pytest.raises(RuntimeError):
        svc.go_to("Камера 2", 10, 10)
    with pytest.raises(RuntimeError):
        svc.scan("Камера 2").result()
    assert svc.backend.snapshot("Камера 2") is None
    svc.go_to("Камера 1", 10, 10)                          # демо-камера працює
    cm.set_demo(conn, 2, servos=True)
    svc.reload()
    svc.start()
    assert b.is_demo("Камера 2")
    svc.shutdown()
