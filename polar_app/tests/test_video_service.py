import time

import pytest

from polar_app.control.service import ControlService
from polar_app.control.sim import SimControlBackend
from polar_app.demo import seed_demo
from polar_app.video_service import VideoService
from polar_core import cameras as cm
from polar_core import db
from polar_core.secrets import SecretBox

import fakes


def wait(seconds, until):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "polar.sqlite"
    conn = db.connect(path, check_same_thread=False)
    seed_demo(conn)
    backend = SimControlBackend()
    ctl = ControlService(conn, backend)
    ctl.start()
    vs = fakes.bind(VideoService(ctl, SecretBox.for_database(path), **fakes.video_options()))
    yield ctl, vs, backend, conn
    vs.shutdown()
    ctl.shutdown()


def test_every_frame_has_angles(env):
    ctl, vs, backend, conn = env
    assert wait(5, lambda: (vs.stats("Камера 1") or None) and vs.stats("Камера 1").frames >= 20)
    ctl.go_to("Камера 1", 30.0, 20.0, 60.0)
    assert wait(5, lambda: vs.stats("Камера 1").frames >= 60)
    s = vs.stats("Камера 1")
    assert s.frames_without_angles == 0
    assert s.connected and s.fps > 10 and s.latency_ok
    f = vs.latest("Камера 1")
    assert f.has_angles and 0 <= f.angles.azimuth_deg < 360


def test_no_link_means_no_angles(env):
    ctl, vs, backend, conn = env
    assert wait(5, lambda: vs.stats("Камера 2") and vs.stats("Камера 2").frames > 5)
    backend.set_online("Камера 2", False)
    time.sleep(0.8)                                   # кути старіють за межу 0,5 с
    n0 = vs.stats("Камера 2").frames_without_angles
    # імітатор показує останній відомий кадр; кутів для нових кадрів немає
    assert wait(3, lambda: vs.stats("Камера 2").frames_without_angles > n0)
    assert not vs.latest("Камера 2").has_angles


def test_measure_latency_sim(env):
    ctl, vs, backend, conn = env
    assert wait(5, lambda: ctl.status("Камера 3").link_ok and vs.stats("Камера 3").frames > 5)
    ms, quality = vs.measure_latency("Камера 3").result(timeout=30)
    assert ms == pytest.approx(fakes.VIDEO_DELAY_S * 1000, abs=45), (ms, quality)
    assert quality > 0.6
    rec = [r for r in cm.list_cameras(conn) if r.name == "Камера 3"][0]
    assert rec.video_latency_ms == ms


def test_check_connection_sim(env):
    ctl, vs, backend, conn = env
    rec = cm.CameraRecord(name="Нова", protocol="RTSP", address="rtsp://10.0.0.9/1", resolution_x=1280,
                          resolution_y=720, fps=20)
    img, size, fps = vs.check_connection(rec, seconds=1.0).result(timeout=20)
    assert size == (640, 360) and img.shape[:2] == (360, 640)
    assert 10 < fps < 30


def test_check_connection_failure(tmp_path, env):
    ctl, _vs, backend, conn = env
    real = VideoService(ctl, SecretBox.for_database(tmp_path / "x.sqlite"))
    rec = cm.CameraRecord(name="Нема", protocol="HTTP", address="http://127.0.0.1:9/none")
    with pytest.raises(Exception):
        real.check_connection(rec, timeout=3).result(timeout=20)


def test_default_is_real_streams(tmp_path, env):
    ctl, _vs, backend, conn = env
    from polar_app.video_service import real_opener
    real = VideoService(ctl, SecretBox.for_database(tmp_path / "y.sqlite"))
    assert real._open_factory is real_opener and real._usb_lister is None


def test_demo_camera_1_is_real_usb(env):
    ctl, vs, backend, conn = env
    rec = cm.list_cameras(conn)[0]
    assert (rec.protocol, rec.address) == ("USB", "0")
    from polar_core.video import capture_target
    assert capture_target(rec) == 0                     # OpenCV відкриє першу USB-камеру компʼютера
