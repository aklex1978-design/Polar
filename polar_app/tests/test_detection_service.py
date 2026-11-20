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
    s = DetectionSettings(hold_s=1.5, reference_update_min=0, patrol_dwell_s=0)     # огляд — окремий тест
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
    target = SimTarget(66.0, 24.0, size_deg=1.5, brightness=255)
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
    target = SimTarget(66.0, 24.0, size_deg=1.5, brightness=255)
    cam.targets.append(target)
    time.sleep(3.0)
    assert ctx.detection.mode.mode == TRACKING
    assert ctx.detection.camera("Камера 1").result.objects == []
    cam.targets.remove(target)


def test_patrol_suspect_and_follow(ctx):
    """Огляд: камера обходить положення; рух — зупинка (підозра на ціль); обʼєкт біля краю кадру — камера
    повертається за ним; ціль зникла — огляд продовжується."""
    ctx.detection.stop()
    with ctx.control._db_lock:
        save_settings(ctx.conn, DetectionSettings(hold_s=1.5, reference_update_min=0, patrol_dwell_s=0.5,
                                                  patrol_el_min_deg=20, patrol_el_max_deg=20, patrol_step_pct=80,
                                                  follow_edge_pct=25))
    with ctx.control._db_lock:
        save_mask(ctx.conn, ctx.records()[0].id, DetectionMask([]))
    assert ctx.detection.start() == ["Камера 1"]
    cam = ctx.detection.camera("Камера 1")
    assert wait(10, lambda: cam.patrol is not None)
    assert [p for p in cam.patrol.plan] == [pytest.approx((a, 20.0)) for a in (0, 72, 144, 216, 288)]
    seen = set()

    def visits():
        sf = ctx.video.latest("Камера 1")
        if sf is not None and sf.has_angles and not sf.angles.moving:
            seen.add(round(sf.angles.azimuth_deg) % 360)
        return len(seen & {0, 72, 144, 216, 288}) >= 3
    assert wait(30, visits), seen                                       # обходить положення
    assert wait(5, lambda: "огляд: положення" in cam.patrol_text), cam.patrol_text

    fake = ctx.fake.cams["Камера 1"]
    t0 = time.monotonic()                                              # час імітатора — time.monotonic()
    target = SimTarget((110.0 - 4.0 * t0) % 360.0, 21.0, d_az_deg_s=4.0, size_deg=2.0, brightness=255)
    fake.targets.append(target)
    assert wait(40, lambda: cam.suspect is not None), cam.patrol_text   # рух — підозра, огляд стоїть
    assert wait(5, lambda: "ПІДОЗРА НА ЦІЛЬ" in cam.patrol_text), cam.patrol_text   # напис — з потоку камери
    assert ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'suspect'").fetchone()[0] >= 1
    assert wait(40, lambda: cam.patrol.suspect is not None and cam.patrol.suspect.follows >= 1), cam.patrol_text

    def on_target():
        sf = ctx.video.latest("Камера 1")
        az_t = (110.0 + 4.0 * (time.monotonic() - t0)) % 360.0
        return sf is not None and sf.has_angles and abs((sf.angles.azimuth_deg - az_t + 180) % 360 - 180) < 30
    assert wait(10, on_target)                                         # камера тримає ціль у кадрі
    sus = cam.patrol.suspect
    assert wait(10, lambda: sus.motion is not None and 2.0 < sus.motion.az_rate < 6.0), sus.motion  # ~4 °/с
    assert wait(5, lambda: "рух праворуч" in cam.patrol.text() and "через 10 с" in cam.patrol.text()), \
        cam.patrol.text()
    fake.targets.remove(target)
    assert wait(20, lambda: cam.suspect is None and "огляд: положення" in cam.patrol_text)
    assert ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'patrol'").fetchone()[0] >= 1


def test_stop_tracking(ctx):
    ctx.detection.stop()
    assert ctx.detection.mode.mode == IDLE and not ctx.video.is_running("Камера 1")
