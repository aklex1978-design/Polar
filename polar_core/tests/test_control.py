import pytest

from polar_core import control as c
from polar_core import db


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    with conn:
        conn.execute("INSERT INTO cameras (name, resolution_x, resolution_y, fov_x_deg, fov_y_deg, "
                     "north_offset_deg, ros_namespace) VALUES ('Камера 1', 1920, 1080, 60, 34, 30, 'camera_1')")
        conn.execute("INSERT INTO cameras (name, resolution_x, resolution_y, fov_x_deg, fov_y_deg) "
                     "VALUES ('Камера 2', 1920, 1080, 60, 34)")
    return conn


def test_defaults_when_no_servo_rows(conn):
    cams = c.load_control_configs(conn)
    assert [x.name for x in cams] == ["Камера 1", "Камера 2"]
    assert cams[1].namespace == "camera_2"
    assert cams[0].pan.continuous and not cams[0].tilt.continuous
    assert cams[0].validate() == []


def test_save_and_load_roundtrip(conn):
    cfg = c.load_control_configs(conn)[0]
    cfg.port, cfg.baudrate = "/dev/ttyAMA0", 500_000
    cfg.pan.direction, cfg.pan.max_speed_deg_s = -1, 120.0
    cfg.tilt.servo_id, cfg.tilt.min_deg, cfg.tilt.max_deg = 5, 5.0, 85.0
    c.save_control_config(conn, cfg)
    again = c.load_control_configs(conn)[0]
    assert again == cfg
    c.save_zero_offset(conn, cfg.camera_id, "pan", 12.5)
    assert c.load_control_configs(conn)[0].pan.zero_offset_deg == 12.5
    c.save_zero_offset(conn, 2, "tilt", 3.0)            # рядка ще немає — створюється
    assert c.load_control_configs(conn)[1].tilt.zero_offset_deg == 3.0


@pytest.mark.parametrize("change,needle", [
    (lambda k: setattr(k.tilt, "servo_id", 1), "різнитися"),
    (lambda k: setattr(k.pan, "servo_id", 300), "1–253"),
    (lambda k: setattr(k.pan, "max_speed_deg_s", 400), "швидкість"),
    (lambda k: setattr(k.pan, "min_deg", 10), "обидві"),
    (lambda k: setattr(k.tilt, "min_deg", None) or setattr(k.tilt, "max_deg", None), "обовʼязкові"),
    (lambda k: setattr(k, "baudrate", 9600), "швидкість обміну"),
])
def test_validation(conn, change, needle):
    cfg = c.load_control_configs(conn)[0]
    change(cfg)
    errors = cfg.validate()
    assert any(needle in e for e in errors), errors
    with pytest.raises(ValueError):
        c.save_control_config(conn, cfg)


def test_ros_settings(conn):
    s = c.load_ros_settings(conn)
    assert s == c.RosSettings()
    s.domain_id, s.rmw, s.sros_enabled, s.sros_keystore = 42, "rmw_cyclonedds_cpp", True, "/keys"
    s.link_timeout_s, s.interface = 3.5, "eth0"
    c.save_ros_settings(conn, s)
    again = c.load_ros_settings(conn)
    assert again == s
    env = again.env()
    assert env["ROS_DOMAIN_ID"] == "42" and env["ROS_SECURITY_KEYSTORE"] == "/keys"
    assert "eth0" in env["CYCLONEDDS_URI"]
    assert again.namespace("camera_1") == "/polar/camera_1"
    s.domain_id = 200
    with pytest.raises(ValueError):
        c.save_ros_settings(conn, s)


def test_target_conversion_and_limits(conn):
    cfg = c.load_control_configs(conn)[0]           # зміщення від півночі 30°
    assert c.target_to_camera(cfg, 40.0, 95.0) == pytest.approx((10.0, 90.0))
    assert c.target_to_camera(cfg, 10.0, -5.0) == pytest.approx((340.0, 0.0))
    cfg.pan.min_deg, cfg.pan.max_deg = 0.0, 180.0
    assert c.target_to_camera(cfg, 10.0, 10.0)[0] == 180.0
    assert c.camera_to_north(cfg, 350.0) == pytest.approx(20.0)


def test_camera_status(conn):
    cfg = c.load_control_configs(conn)[0]
    assert not c.camera_status(cfg, None, 10.0, 2.0).link_ok
    snap = c.CameraSnapshot(received_at=9.0, azimuth_deg=350.0, elevation_deg=20.0, moving=True,
                            error_flags=c.ERROR_OVERHEAT | c.ERROR_STOPPED)
    st = c.camera_status(cfg, snap, 10.0, 2.0)
    assert st.link_ok and st.link_text == "на звʼязку"
    assert st.azimuth_deg == pytest.approx(20.0)
    assert st.error_text == "перегрів сервопривода, зупинено"
    assert not c.camera_status(cfg, snap, 11.5, 2.0).link_ok
    snap.error_flags = c.ERROR_LINK_TIMEOUT
    assert not c.camera_status(cfg, snap, 10.0, 2.0).link_ok


def test_servo_raw_range_must_fit_position_mode():
    """Вертикальний ST3020 (режим положення) — вал лише 0…360°: зворотний напрямок із нулем 0 недопустимий."""
    from polar_core.control import ServoSettings
    ok = ServoSettings("tilt", 2, direction=1, zero_offset_deg=0.0, min_deg=0.0, max_deg=90.0)
    assert ok.validate() == [] and ok.raw_range() == (0.0, 90.0)
    bad = ServoSettings("tilt", 2, direction=-1, zero_offset_deg=0.0, gear_ratio=0.6, min_deg=4.0, max_deg=90.0)
    assert any("0…360" in e for e in bad.validate())
    fixed = ServoSettings("tilt", 2, direction=-1, zero_offset_deg=180.0, min_deg=0.0, max_deg=90.0)
    assert fixed.validate() == [] and fixed.raw_range() == (90.0, 180.0)
    assert ServoSettings("pan", 1, direction=-1).raw_range() == (None, None)          # ковзний контакт


def test_limit_notes():
    from polar_core.control import CameraControlConfig, ServoSettings, limit_notes
    cfg = CameraControlConfig(1, "К", "camera_1",
                              pan=ServoSettings("pan", 1, min_deg=10.0, max_deg=350.0),
                              tilt=ServoSettings("tilt", 2, min_deg=4.0, max_deg=90.0))
    assert limit_notes(cfg, 5.0, 95.0) == ["азимут — нижня межа 10°", "кут місця — верхня межа 90°"]
    assert limit_notes(cfg, 100.0, 4.0) == []
    assert limit_notes(cfg, None, 1.0) == ["кут місця — нижня межа 4°"]


def test_freed_limits_cover_full_shaft_travel():
    """«Зняти межі»: межі осі — на весь хід вала 0…360° від поточного нуля; ковзний контакт без змін."""
    from polar_core.control import CameraControlConfig, ServoSettings
    t = ServoSettings("tilt", 2, direction=1, zero_offset_deg=30.0, min_deg=0.0, max_deg=90.0)
    f = t.freed()
    assert (f.min_deg, f.max_deg) == (-30.0, 330.0) and f.validate() == [] and t.max_deg == 90.0
    r = ServoSettings("tilt", 2, direction=-1, zero_offset_deg=200.0, min_deg=0.0, max_deg=90.0).freed()
    assert (r.min_deg, r.max_deg) == (-160.0, 200.0)
    cfg = CameraControlConfig(1, "К", "camera_1", tilt=t)
    assert cfg.freed().tilt.min_deg == -30.0 and cfg.freed().pan.min_deg is None and cfg.tilt.min_deg == 0.0
