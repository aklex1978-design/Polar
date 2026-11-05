import pytest

from polar_core import calibration as cal
from polar_core import cameras as cm
from polar_core import db
from polar_core.secrets import SecretBox


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "p.sqlite"
    conn = db.connect(path)
    rec = cm.CameraRecord(name="К1", latitude=50, longitude=30, altitude_m=100, address="rtsp://x/1",
                          ros_namespace="camera_1", stream_res_x=1920, stream_res_y=1080, k1=-0.1, tilt_deg=2.0,
                          north_offset_deg=15.0)
    cid, _ = cm.save_camera(conn, rec, SecretBox.for_database(path))
    return conn, cm.get_camera(conn, cid)


def test_center_save_load_and_scale(env):
    conn, rec = env
    assert cal.load(conn, rec.id).center_x is None
    m = cal.model_for(conn, rec)
    assert (m.cx, m.cy) == (960, 540) and m.k1 == -0.1 and m.tilt_deg == 2.0 and m.north_offset_deg == 15.0
    cal.save_center(conn, rec.id, 1000.0, 520.0, (1920, 1080))
    c = cal.load(conn, rec.id)
    assert (c.center_x, c.center_y, c.frame_w) == (1000.0, 520.0, 1920) and c.updated_at
    m = cal.model_for(conn, rec, frame_size=(640, 360))           # потік віддає менші кадри
    assert m.cx == pytest.approx(1000 / 3) and m.cy == pytest.approx(520 / 3) and m.width == 640


def test_placement_error_and_criterion(env):
    conn, rec = env
    assert cal.accuracy_criterion(conn) == 0.5
    cal.save_placement_error(conn, rec.id, 0.21, (1920, 1080))
    assert cal.load(conn, rec.id).placement_error_deg == 0.21
    cal.save_center(conn, rec.id, 950, 545, (1920, 1080))
    assert cal.load(conn, rec.id).placement_error_deg == 0.21      # центр не стирає похибку
    with conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('detection.accuracy_deg', '0.3')")
    assert cal.accuracy_criterion(conn) == 0.3


def test_landmark_check():
    lc = cal.LandmarkCheck()
    assert lc.error() is None
    lc.add(359.9, 10.0)
    lc.add(0.1, 10.0)                                                 # через 0/360
    assert lc.error() == pytest.approx(0.1 * 0.9848, abs=1e-3)       # 0,1° азимуту на куті місця 10°
    assert lc.mean()[0] == pytest.approx(0.0, abs=1e-6) or lc.mean()[0] == pytest.approx(360.0, abs=1e-6)
    lc.add(0.0, 10.6)
    assert lc.error() == pytest.approx(0.4, abs=0.02)
    lc.clear()
    assert lc.observations == []
