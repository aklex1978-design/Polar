"""Наскрізна перевірка через rosbridge: Polar (RosbridgeControlBackend, без rclpy) ↔ rosbridge_server ↔
pantilt_driver (імітатор) — так, як сервер на Windows працює зі справжнім Raspberry Pi.

Потрібні ROS 2 Humble, ros-humble-rosbridge-server і зібраний ros2_ws (source install/setup.bash).
Запуск: pytest polar_app/tests_ros
"""
import os
import shutil
import signal
import subprocess
import time

import pytest

if shutil.which("ros2") is None:
    pytest.skip("потрібен ROS 2 (ros2 у PATH)", allow_module_level=True)

from polar_app.control.rosbridge import RosbridgeControlBackend  # noqa: E402
from polar_app.control.service import ControlService  # noqa: E402
from polar_core import control as ctl  # noqa: E402
from polar_core import db  # noqa: E402

PORT = 9191


def wait(seconds, until):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def pi():
    procs = [
        subprocess.Popen(["ros2", "run", "pantilt_driver", "pantilt_node", "--ros-args",
                          "-r", "__ns:=/polar/camera_7", "-p", "backend:=sim", "-p", "link_timeout_s:=1.0"],
                         start_new_session=True),
        subprocess.Popen(["ros2", "launch", "rosbridge_server", "rosbridge_websocket_launch.xml",
                          f"port:={PORT}"], start_new_session=True),
    ]
    yield procs
    for p in procs:
        os.killpg(p.pid, signal.SIGINT)
    for p in procs:
        p.wait(timeout=15)


@pytest.fixture(scope="module")
def svc(pi, tmp_path_factory):
    conn = db.connect(tmp_path_factory.mktemp("db") / "polar.sqlite", check_same_thread=False)
    with conn:
        conn.execute("INSERT INTO cameras (name, resolution_x, resolution_y, fov_x_deg, fov_y_deg, north_offset_deg, "
                     "ros_namespace, stream_url) VALUES ('Камера 7', 720, 480, 24, 18, 10, 'camera_7', "
                     "'rtsp://127.0.0.1:8554/camera_7')")                  # адреса Pi — з адреси потоку
    s = ctl.load_ros_settings(conn)
    s.bridge_port = PORT
    ctl.save_ros_settings(conn, s)
    service = ControlService(conn, RosbridgeControlBackend())
    service.start()
    yield service
    service.shutdown()


def test_link_and_configure(svc):
    assert wait(40, lambda: svc.status("Камера 7").link_ok), svc.backend.connection_info("Камера 7")
    assert wait(15, lambda: svc.last_configure.get("Камера 7", (False,))[0]), svc.last_configure


def test_go_to_jog_and_stop(svc):
    svc.go_to("Камера 7", 100.0, 30.0, 180.0)                 # кут камери 90° (зміщення від півночі 10°)
    assert wait(8, lambda: abs(svc.status("Камера 7").azimuth_deg - 100.0) < 0.1
                and abs(svc.status("Камера 7").elevation_deg - 30.0) < 0.1)
    svc.jog("Камера 7", 0.0, 10.0, 90.0)
    assert wait(8, lambda: abs(svc.status("Камера 7").elevation_deg - 40.0) < 0.1)
    assert svc.stop("Камера 7").result(timeout=5)[0]
    assert wait(3, lambda: svc.status("Камера 7").error_flags & ctl.ERROR_STOPPED)


def test_services(svc):
    assert svc.scan("Камера 7", 1, 5).result(timeout=25) == [1, 2]
    zero = svc.set_zero("Камера 7", "tilt").result(timeout=5)
    assert zero == pytest.approx(40.0, abs=0.2)
    cfg = svc.config("Камера 7")
    cfg.tilt.max_deg = 45.0
    assert svc.save_config(cfg).result(timeout=5) == (True, "налаштування застосовано")
