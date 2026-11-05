import pytest

from polar_core import cameras as cm
from polar_core import control, db
from polar_core.secrets import SecretBox


def rec(**kw):
    base = dict(name="Камера 1", latitude=50.45, longitude=30.52, altitude_m=180.0, protocol="RTSP",
                address="rtsp://192.168.1.10:554/stream1", ros_namespace="camera_1")
    base.update(kw)
    return cm.CameraRecord(**base)


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "polar.sqlite"
    return db.connect(path), SecretBox.for_database(path), path


def test_valid_record():
    assert rec().validate() == {}


@pytest.mark.parametrize("kw,field", [
    (dict(name=" "), "name"),
    (dict(resolution_x=0), "resolution_x"),
    (dict(fov_x_deg=200), "fov_x_deg"),
    (dict(latitude=None), "latitude"),
    (dict(longitude=181), "longitude"),
    (dict(north_offset_deg=360), "north_offset_deg"),
    (dict(tilt_deg=-181), "tilt_deg"),
    (dict(address="http://x/y"), "address"),
    (dict(address="rtsp://admin:1234@cam/x"), "address"),
    (dict(protocol="USB", address="cam"), "address"),
    (dict(fps=0), "fps"),
    (dict(buffer_ms=-1), "buffer_ms"),
    (dict(ros_namespace="камера"), "ros_namespace"),
    (dict(az_min=10.0), "az_min"),
    (dict(az_min=100.0, az_max=50.0), "az_min"),
    (dict(el_min=50.0, el_max=40.0), "el_min"),
    (dict(max_speed_deg_s=0), "max_speed_deg_s"),
])
def test_field_errors(kw, field):
    assert field in rec(**kw).validate()


@pytest.mark.parametrize("proto,addr", [("USB", "0"), ("USB", "/dev/video2"), ("HTTP", "http://cam/mjpg"),
                                        ("ONVIF", "192.168.1.20:8000"), ("RTSP", "rtsps://cam:322/a")])
def test_addresses_ok(proto, addr):
    assert cm.address_error(proto, addr) is None


def test_save_load_and_password(env):
    conn, box, path = env
    r = rec(password="таємниця", login="admin", az_min=10.0, az_max=350.0, el_min=5.0, el_max=80.0,
            max_speed_deg_s=90.0, stream_res_x=1280, stream_res_y=720)
    cid, stale = cm.save_camera(conn, r, box)
    assert not stale
    got = cm.get_camera(conn, cid)
    assert got.has_password and got.password is None and got.login == "admin"
    assert (got.az_min, got.az_max, got.el_min, got.el_max, got.max_speed_deg_s) == (10, 350, 5, 80, 90)
    assert got.frame_size == (1280, 720)
    assert cm.stream_password(conn, box, cid) == "таємниця"
    raw = conn.execute("SELECT stream_password_enc FROM cameras").fetchone()[0]
    assert "таємниця" not in raw
    # той самий ключ після перезапуску
    assert cm.stream_password(conn, SecretBox.for_database(path), cid) == "таємниця"
    # налаштування керування бачать межі
    cfg = control.load_control_configs(conn)[0]
    assert cfg.pan.min_deg == 10 and cfg.tilt.max_deg == 80 and not cfg.pan.continuous


def test_unique_name_and_namespace(env):
    conn, box, _ = env
    cm.save_camera(conn, rec(), box)
    errs = rec(name="камера 1", ros_namespace="/camera_1").validate(cm.list_cameras(conn))
    assert "name" in errs and "ros_namespace" in errs
    with pytest.raises(ValueError):
        cm.save_camera(conn, rec(name="Камера 1"), box)


def test_optics_change_marks_panorama_stale(env):
    conn, box, _ = env
    cid, _ = cm.save_camera(conn, rec(), box)
    r = cm.get_camera(conn, cid)
    r.model = "інша"
    assert cm.save_camera(conn, r, box) == (cid, False)
    r.fov_x_deg = 62.0
    assert cm.save_camera(conn, r, box) == (cid, True)
    assert cm.get_camera(conn, cid).panorama_stale
    assert cm.stream_password(conn, box, cid) == ""        # пароль не задавали


def test_delete(env):
    conn, box, _ = env
    cid, _ = cm.save_camera(conn, rec(panorama_path="/tmp/p1.tif"), box)
    with conn:
        conn.execute("INSERT INTO events (camera_id, kind) VALUES (?, 'x')", (cid,))
    assert cm.delete_camera(conn, cid) == "/tmp/p1.tif"
    assert conn.execute("SELECT COUNT(*) FROM servo_settings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    with pytest.raises(KeyError):
        cm.delete_camera(conn, cid)


def test_wrong_key(env):
    conn, box, _ = env
    cid, _ = cm.save_camera(conn, rec(password="x"), box)
    with pytest.raises(ValueError):
        cm.stream_password(conn, SecretBox.for_database(":memory:"), cid)
