"""Етап 6: сервіс цілей — обʼєкти камер → промені → цілі з GPS у таблиці targets і на екрані."""
import time
from types import SimpleNamespace as NS

import pytest

from polar_app.__main__ import build_context
from polar_core import db
from polar_core.detection import DetectionSettings
from polar_core.geo import GeoPoint, LocalFrame, bearing_elevation

import fakes
from demo_data import seed_demo


def no_ros():
    raise ImportError("No module named 'rclpy'")


@pytest.fixture
def ctx(tmp_path):
    path = str(tmp_path / "polar.sqlite")
    conn = db.connect(path)
    seed_demo(conn)
    c = fakes.bind(build_context(path, video_options=fakes.video_options(), ros_factory=no_ros))
    yield c
    c.shutdown()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def observe(svc, recs, target: GeoPoint, t, track=1, skip=()):
    frame = LocalFrame.around(GeoPoint(r.latitude, r.longitude, r.altitude_m) for r in recs)
    for r in recs:
        if r.name in skip:
            svc.offer(r, [], t)
            continue
        az, el, _ = bearing_elevation(frame, GeoPoint(r.latitude, r.longitude, r.altitude_m), target)
        svc.offer(r, [(NS(id=track), NS(az=az, el=el))], t)


def test_target_confirmed_written_and_lost(ctx):
    from polar_app.target_service import TargetService
    clock = Clock()
    svc = TargetService(ctx, clock)
    recs = ctx.records()
    s = DetectionSettings()
    s.hold_s = 2.0
    svc.start(recs, s)
    svc._stop.set()                       # кроки — вручну, з тестовим годинником
    svc._thread.join()
    target = GeoPoint(50.4515, 30.5270, 450.0)
    for i in range(3):
        clock.t += 0.04
        observe(svc, recs, target, clock.t)
        found = svc.step()
        assert (len(found) == 1) == (i == 2)                        # підтвердження — 3 кадри поспіль
    tt = found[0]
    assert sorted(tt.target.cameras) == sorted(r.name for r in recs)
    frame = LocalFrame.around([target])
    assert float(((frame.to_enu(tt.geo)) ** 2).sum()) ** 0.5 < 0.1         # GPS цілі
    row = ctx.conn.execute("SELECT latitude, longitude, altitude_m, cameras, status FROM targets").fetchone()
    assert row[0] == pytest.approx(50.4515, abs=1e-6) and row[2] == pytest.approx(450, abs=0.1)
    assert row[3] == 3 and row[4] == "active"
    rows = svc.rows()
    assert len(rows) == 3 and all(r.get("target") == tt.number for r in rows)
    r1 = next(r for r in rows if r["camera"] == recs[0].name)
    assert r1["dist"] == pytest.approx(bearing_elevation(frame, GeoPoint(recs[0].latitude, recs[0].longitude,
                                                                          recs[0].altitude_m), target)[2], abs=0.1)
    # одна камера ціль не бачить: її рядок порожній, ціль тримається на двох
    clock.t += 0.04
    observe(svc, recs, target, clock.t, skip=(recs[2].name,))
    assert len(svc.step()) == 1
    rows = svc.rows()
    assert rows[2] == {"camera": recs[2].name}
    assert svc.targets[tt.number].target.cameras and len(svc.targets[tt.number].target.rays) == 2
    # ціль зникла: після часу утримання — «lost»
    for _ in range(3):
        clock.t += 1.0
        for r in recs:
            svc.offer(r, [], clock.t)
        svc.step()
    assert not svc.targets
    assert ctx.conn.execute("SELECT status FROM targets").fetchone()[0] == "lost"
    assert ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'target_lost'").fetchone()[0] == 1


def test_target_motion_course_speed_and_prediction(ctx):
    """Ціль летить на схід 20 м/с і набирає 3 м/с: курс 90°, швидкості, прогноз координат через 10 с."""
    import numpy as np

    from polar_app.target_service import TargetService
    clock = Clock()
    svc = TargetService(ctx, clock)
    recs = ctx.records()
    s = DetectionSettings(hold_s=2.0, motion_window_s=2.0, predict_s=10.0)
    svc.start(recs, s)
    svc._stop.set()
    svc._thread.join()
    start = GeoPoint(50.4515, 30.5270, 450.0)
    local = LocalFrame(start)
    v = np.array([20.0, 0.0, 3.0])
    for i in range(50):                                   # 2 с по 0,04 с
        clock.t += 0.04
        observe(svc, recs, local.to_geo(v * (i * 0.04)), clock.t)
        svc.step()
    tt = next(iter(svc.targets.values()))
    m = tt.motion
    assert m.course_deg == pytest.approx(90.0, abs=1.0)
    assert m.speed_mps == pytest.approx(20.0, abs=0.5) and m.climb_mps == pytest.approx(3.0, abs=0.3)
    expect = local.to_geo(v * (49 * 0.04 + 10.0))
    assert np.linalg.norm(local.to_enu(tt.predicted) - local.to_enu(expect)) < 5.0
    row = next(r for r in svc.rows() if r["camera"] == recs[0].name)
    assert row["course"] == pytest.approx(90.0, abs=1.0) and row["pred_alt"] == pytest.approx(expect.altitude_m, abs=2)
    db_row = ctx.conn.execute("SELECT course_deg, speed_mps, climb_mps, predict_s, pred_latitude, pred_longitude, "
                              "pred_altitude_m FROM targets").fetchone()
    assert db_row[0] == pytest.approx(90.0, abs=1.0) and db_row[3] == 10.0 and db_row[4] is not None


def test_single_camera_gives_no_coordinates(ctx):
    from polar_app.target_service import TargetService
    clock = Clock()
    svc = TargetService(ctx, clock)
    recs = ctx.records()
    svc.start(recs, DetectionSettings())
    svc._stop.set()
    svc._thread.join()
    for _ in range(5):
        clock.t += 0.04
        observe(svc, recs, GeoPoint(50.4515, 30.5270, 450.0), clock.t, skip=(recs[1].name, recs[2].name))
        assert svc.step() == []
    row = next(r for r in svc.rows() if r["camera"] == recs[0].name)
    assert "az" in row and "lat" not in row                          # кути є, GPS — ні
    assert ctx.conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 0


def wait(seconds, until):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until():
            return True
        time.sleep(0.05)
    return False


def test_end_to_end_two_cameras_find_gps_of_test_target(tmp_path):
    """Критерій етапу наскрізно: дві камери (демо-сервоприводи, синтетичне відео) навчаються, наводяться
    на тестову ціль, виявляють її на кадрах — і координати цілі збігаються з її GPS у межах допуску."""
    from polar_core.sim.camera import SimTarget
    path = str(tmp_path / "polar.sqlite")
    conn = db.connect(path)
    seed_demo(conn)
    with conn:
        conn.execute("DELETE FROM cameras WHERE name = 'Камера 3'")
        conn.execute("UPDATE cameras SET fov_x_deg = 90, fov_y_deg = 60, stream_res_x = 640, stream_res_y = 480, "
                     "north_offset_deg = 0, altitude_m = 180")
        conn.execute("UPDATE servo_settings SET max_speed_deg_s = 360")
    opts = fakes.video_options()
    c = fakes.bind(build_context(path, video_options=opts, ros_factory=no_ros))
    c.learning.max_px_per_deg = 4.0
    try:
        recs = c.records()
        assert wait(5, lambda: all(c.control.status(r.name).link_ok for r in recs))
        c.start_learning([r.name for r in recs])
        for r in recs:
            assert c.learning.wait(r.name, timeout=200).state == "done"
        assert wait(10, lambda: c.detection.active)        # відео перевідкрито для Слідкування — сцена нова
        time.sleep(0.5)
        target = GeoPoint(50.4540, 30.5260, 330.0)
        frame = LocalFrame.around(GeoPoint(r.latitude, r.longitude, r.altitude_m) for r in recs)
        for r in recs:
            pos = GeoPoint(r.latitude, r.longitude, r.altitude_m)
            az, el, _ = bearing_elevation(frame, pos, target)
            c.control.go_to(r.name, az + 3.0, el - 2.0, source="learning")       # ціль — не в центрі кадру
            opts["open_factory"].cams[r.name].targets.append(SimTarget(az, el, size_deg=1.2, brightness=255))
        assert wait(25, lambda: c.detection.targets.targets)
        tt = next(iter(c.detection.targets.targets.values()))
        err = float((frame.to_enu(tt.geo) - frame.to_enu(target)) @ (frame.to_enu(tt.geo) - frame.to_enu(target))) ** 0.5
        tol = c.detection.targets.params.tolerance(tt.target.distance_m)
        assert err <= tol, (err, tol)
        q = "SELECT latitude, longitude, altitude_m, cameras FROM targets"
        def stored():
            with c.control._db_lock:
                return c.conn.execute(q).fetchone() is not None
        assert wait(10, stored)                            # запис у базу — асинхронно
        row = c.conn.execute(q).fetchone()
        assert row[3] == 2 and row[2] == pytest.approx(330.0, abs=tol)

        # огляд + наведення: камери самі обходять зону; та, що першою побачить ціль, — підозра, інша
        # шукає уздовж її променя (або одразу наводиться на ціль з координатами)
        import numpy as np

        from polar_core.detection import save_settings
        from polar_core.patrol import CUE
        c.detection.stop()
        target = GeoPoint(50.4470, 30.5200, 350.0)                    # нова ціль: стару еталон уже міг «вивчити»
        sims = {}
        for r in recs:
            sims[r.name] = bearing_elevation(frame, GeoPoint(r.latitude, r.longitude, r.altitude_m), target)[:2]
        els = [el for _az, el in sims.values()]
        with c.control._db_lock:
            save_settings(c.conn, DetectionSettings(hold_s=3.0, reference_update_min=0, patrol_dwell_s=0.5,
                                                    patrol_el_min_deg=round(min(els)), patrol_el_max_deg=round(min(els)),
                                                    follow_center_pct=10, cue_others=1, cue_min_range_m=50))
            c.conn.execute("DELETE FROM targets")
        old = {r.name: opts["open_factory"].cams.get(r.name) for r in recs}
        assert sorted(c.detection.start()) == sorted(r.name for r in recs)
        assert "__cue__" in c.detection._threads
        assert wait(10, lambda: all(opts["open_factory"].cams.get(r.name) is not old[r.name] for r in recs))
        a_name, b_name = recs[0].name, recs[1].name
        # спершу ціль бачить лише камера A (відео перепідключено — нові камери імітатора)
        opts["open_factory"].cams[a_name].targets[:] = [SimTarget(*sims[a_name], size_deg=1.2, brightness=255)]
        cam_a, cam_b = c.detection.camera(a_name), c.detection.camera(b_name)
        assert wait(60, lambda: cam_a.suspect is not None), (cam_a.patrol_text, cam_b.patrol_text)
        assert wait(10, lambda: cam_b.patrol.state == CUE), cam_b.patrol_text       # B шукає по променю A
        assert wait(5, lambda: "НАВЕДЕННЯ: пошук по променю «" + a_name in cam_b.patrol_text), cam_b.patrol_text
        with c.control._db_lock:
            assert c.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'cue'").fetchone()[0] >= 1
        opts["open_factory"].cams[b_name].targets[:] = [SimTarget(*sims[b_name], size_deg=1.2, brightness=255)]
        assert wait(60, lambda: c.detection.targets.targets), (cam_a.patrol_text, cam_b.patrol_text)
        tt = next(iter(c.detection.targets.targets.values()))
        err = float(np.linalg.norm(frame.to_enu(tt.geo) - frame.to_enu(target)))
        assert err <= c.detection.targets.params.tolerance(tt.target.distance_m), err
    finally:
        c.shutdown()
