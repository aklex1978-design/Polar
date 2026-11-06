"""Слідкування → Виявлення → Слідкування на тестовій камері (демо-сервоприводи, тестове відео)."""
import time

import pytest

from polar_app.__main__ import build_context
from polar_core import db
from polar_core.detection import DETECTION, IDLE, TRACKING, DetectionSettings, save_settings
from polar_core.mask import DetectionMask, MaskItem, save_mask
from polar_core.sim import SimTarget

import fakes
from demo_data import seed_demo


def no_ros():
    raise ImportError("No module named 'rclpy'")


def wait(seconds, until):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("d") / "polar.sqlite")
    conn = db.connect(path)
    seed_demo(conn)
    with conn:
        conn.execute("UPDATE cameras SET fov_x_deg = 90, fov_y_deg = 60, stream_res_x = 640, stream_res_y = 360")
        conn.execute("UPDATE servo_settings SET max_speed_deg_s = 360")
        conn.execute("DELETE FROM cameras WHERE name != 'Камера 1'")
    s = DetectionSettings(hold_s=1.5, reference_update_min=0)
    save_settings(conn, s)
    opts = fakes.video_options()
    c = fakes.bind(build_context(path, video_options=opts, ros_factory=no_ros))
    c.learning.max_px_per_deg = 4.0
    c.fake = opts["open_factory"]
    assert wait(5, lambda: c.control.status("Камера 1").link_ok)
    c.start_learning(["Камера 1"])
    assert c.learning.wait("Камера 1", timeout=150).state == "done"
    yield c
    c.shutdown()


def test_learning_switches_to_tracking(ctx):
    """ТЗ: після Навчання система сама переходить у Слідкування."""
    assert wait(10, lambda: ctx.detection.mode.mode == TRACKING)
    ctx.control.go_to("Камера 1", 60.0, 20.0, source="learning")
    assert wait(10, lambda: (ctx.detection.camera("Камера 1").result is not None
                             and ctx.detection.camera("Камера 1").result.compared
                             and abs(ctx.video.latest("Камера 1").angles.azimuth_deg - 60.0) < 0.5))
    time.sleep(1.0)
    assert ctx.detection.camera("Камера 1").result.objects == []          # без цілі — руху немає
    assert ctx.detection.mode.mode == TRACKING


def test_target_switches_to_detection_and_back(ctx):
    cam = ctx.fake.cams["Камера 1"]
    target = SimTarget(66.0, 24.0, size_deg=1.5, brightness=0)
    cam.targets.append(target)
    assert wait(6, lambda: ctx.detection.mode.mode == DETECTION)
    objs = ctx.detection.camera("Камера 1").result.objects
    assert len(objs) == 1
    assert abs(objs[0].az - 66.0) < 0.5 and abs(objs[0].el - 24.0) < 0.5     # зміщення від півночі 0
    rows = ctx.conn.execute("SELECT azimuth_deg, elevation_deg FROM detections").fetchall()
    assert rows and abs(rows[0][0] - 66.0) < 0.5
    assert ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'motion'").fetchone()[0] == 1
    lat = ctx.detection.camera("Камера 1").latency
    assert lat.percentile() is not None and not lat.exceeded
    cam.targets.remove(target)
    assert wait(6, lambda: ctx.detection.mode.mode == TRACKING)           # після часу утримання


def test_dead_zone_ignored(ctx):
    m = DetectionMask([MaskItem.rect(60, 18, 75, 30)])
    cid = ctx.records()[0].id
    with ctx.control._db_lock:
        save_mask(ctx.conn, cid, m)
    time.sleep(2.5)                                                       # маску перечитує раз на 2 с
    cam = ctx.fake.cams["Камера 1"]
    target = SimTarget(66.0, 24.0, size_deg=1.5, brightness=0)
    cam.targets.append(target)
    time.sleep(3.0)
    assert ctx.detection.mode.mode == TRACKING
    assert ctx.detection.camera("Камера 1").result.objects == []
    cam.targets.remove(target)


def test_stop_tracking(ctx):
    ctx.detection.stop()
    assert ctx.detection.mode.mode == IDLE and not ctx.video.is_running("Камера 1")
