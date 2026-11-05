"""Наскрізна перевірка через ROS 2: polar_control (RosControlBackend) ↔ pantilt_driver (імітатор).

Потрібні ROS 2 Humble і зібраний ros2_ws (source install/setup.bash). Запуск: pytest polar_app/tests_ros
"""
import os
import signal
import subprocess
import time

import pytest

rclpy = pytest.importorskip("rclpy")

from polar_app.control.ros import RosControlBackend  # noqa: E402
from polar_app.control.service import ControlService  # noqa: E402
from polar_core import control as ctl  # noqa: E402
from polar_core import db  # noqa: E402


def wait(seconds, until):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def driver():
    proc = subprocess.Popen(
        ["ros2", "run", "pantilt_driver", "pantilt_node", "--ros-args",
         "-r", "__ns:=/polar/camera_1", "-p", "backend:=sim", "-p", "link_timeout_s:=1.0"],
        start_new_session=True)
    yield proc
    os.killpg(proc.pid, signal.SIGINT)
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def svc(driver, tmp_path_factory):
    conn = db.connect(tmp_path_factory.mktemp("db") / "polar.sqlite", check_same_thread=False)
    with conn:
        conn.execute("INSERT INTO cameras (name, resolution_x, resolution_y, fov_x_deg, fov_y_deg, "
                     "north_offset_deg, ros_namespace) VALUES ('Камера 1', 1920, 1080, 60, 34, 10, 'camera_1')")
    s = ControlService(conn, RosControlBackend())
    s.start()
    yield s
    s.shutdown()


def test_link_and_configure(svc):
    assert wait(20, lambda: svc.status("Камера 1").link_ok)
    assert wait(10, lambda: svc.last_configure.get("Камера 1", (False,))[0]), svc.last_configure


def test_go_to_and_stop(svc):
    svc.go_to("Камера 1", 100.0, 30.0, 180.0)      # кут камери 90°
    assert wait(5, lambda: abs(svc.status("Камера 1").azimuth_deg - 100.0) < 0.1
                and abs(svc.status("Камера 1").elevation_deg - 30.0) < 0.1)
    svc.jog("Камера 1", 0.0, 10.0, 90.0)
    assert wait(5, lambda: abs(svc.status("Камера 1").elevation_deg - 40.0) < 0.1)
    assert svc.stop("Камера 1").result(timeout=5)[0]
    assert wait(2, lambda: svc.status("Камера 1").error_flags & ctl.ERROR_STOPPED)


def test_services(svc):
    assert svc.scan("Камера 1", 1, 5).result(timeout=25) == [1, 2]
    zero = svc.set_zero("Камера 1", "tilt").result(timeout=5)
    assert zero == pytest.approx(40.0, abs=0.2)
    assert wait(3, lambda: abs(svc.status("Камера 1").elevation_deg) < 0.2)
    cfg = svc.config("Камера 1")
    cfg.tilt.max_deg = 45.0
    assert svc.save_config(cfg).result(timeout=5) == (True, "налаштування застосовано")
