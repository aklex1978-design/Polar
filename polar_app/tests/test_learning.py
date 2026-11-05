"""Навчання: камери обходять 360° × 90° і записують панораму (демо-сервоприводи, тестове відео)."""
import os
import time

import pytest

from polar_app.__main__ import build_context
from polar_core import cameras as cm
from polar_core import db
from polar_core.panorama import Panorama

import fakes
from demo_data import seed_demo


def no_ros():
    raise ImportError("No module named 'rclpy'")


@pytest.fixture
def ctx(tmp_path):
    path = str(tmp_path / "polar.sqlite")
    conn = db.connect(path)
    seed_demo(conn)
    with conn:   # широкий кут огляду — менше кроків, тест швидший
        conn.execute("UPDATE cameras SET fov_x_deg = 90, fov_y_deg = 60, stream_res_x = 640, stream_res_y = 360")
        conn.execute("UPDATE servo_settings SET max_speed_deg_s = 360")
    c = fakes.bind(build_context(path, video_options=fakes.video_options(), ros_factory=no_ros))
    c.learning.max_px_per_deg = 3.0
    yield c
    c.shutdown()


def wait(seconds, until):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until():
            return True
        time.sleep(0.05)
    return False


def test_learning_builds_full_panorama(ctx):
    assert wait(5, lambda: ctx.control.status("Камера 1").link_ok)
    assert ctx.learning.start(["Камера 1"]) == ["Камера 1"]
    assert ctx.control.is_learning("Камера 1")
    with pytest.raises(Exception):
        ctx.control.go_to("Камера 1", 10, 10)                 # ручне керування заблоковане
    st = ctx.learning.wait("Камера 1", timeout=120)
    assert st.state == "done", (st.state, st.message)
    # 8 по азимуту × 4 ряди: кадр 640×360 при HFOV 90° має фактичний VFOV 58,7° < 60°, крок 29,4°
    assert st.steps == 32 and not st.skipped
    pano = ctx.learning.panorama("Камера 1")
    assert pano.coverage() == 1.0 and pano.gaps() == 0       # критерій: 360° × 90° без пропусків
    rec = cm.get_camera(ctx.conn, ctx.records()[0].id)
    assert Panorama.exists(rec.panorama_path) and not rec.panorama_stale
    assert os.path.dirname(rec.panorama_path).endswith("panoramas")
    ctx.learning.forget("Камера 1")
    assert ctx.learning.panorama("Камера 1").coverage() == 1.0     # відновлюється з файлу
    assert not ctx.control.is_learning("Камера 1") and not ctx.video.is_running("Камера 1")
    kinds = [r[0] for r in ctx.conn.execute("SELECT kind FROM events")]
    assert "learning_done" in kinds


def test_learning_stop_and_no_link(ctx):
    assert wait(5, lambda: ctx.control.status("Камера 2").link_ok)
    ctx.control.backend.sim.set_online("Камера 3", False)
    assert wait(5, lambda: not ctx.control.status("Камера 3").link_ok)
    started = ctx.learning.start()
    assert "Камера 2" in started and "Камера 3" not in started
    assert ctx.learning.status("Камера 3").state == "error"
    assert wait(15, lambda: ctx.learning.status("Камера 2").step >= 3)
    ctx.control.stop("Камера 2").result(timeout=5)              # «Стоп» на екрані керування теж зупиняє
    st = ctx.learning.wait("Камера 2", timeout=20)
    assert st.state == "stopped"
    assert 0 < ctx.learning.panorama("Камера 2").coverage() < 1.0
    ctx.learning.stop()


def test_manual_input_writes_frame(ctx):
    assert ctx.video.acquire("Камера 1")
    assert wait(5, lambda: ctx.video.latest("Камера 1") is not None)
    sf = ctx.video.latest("Камера 1")
    cov = ctx.learning.add_manual("Камера 1", sf.image, 40.0, 20.0)
    assert 0 < cov < 0.2
    rec = cm.get_camera(ctx.conn, ctx.records()[0].id)
    assert Panorama.exists(rec.panorama_path)
    ctx.video.release("Камера 1")
