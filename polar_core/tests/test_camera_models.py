"""Моделі камер: міграція наявних камер, збереження моделі оновлює камери, камера бере оптику з моделі."""
import sqlite3

import pytest

from polar_core import camera_models as cmod
from polar_core import cameras as cm
from polar_core import db
from cryptography.fernet import Fernet

from polar_core.secrets import SecretBox


def _old_db(path):
    """База до міграції 8 (як у встановлених системах) з камерами різних моделей."""
    conn = sqlite3.connect(path)
    for i in range(7):
        conn.executescript(db.MIGRATIONS[i])
    conn.execute("PRAGMA user_version = 7")
    rows = [("К1", "Caddx 256", 60, 34, 0.0), ("К2", "Caddx 256", 60, 34, 0.0),   # одна модель
            ("К3", "Caddx 256", 25, 19, -0.1),                                   # та сама назва, інша оптика
            ("К4", "", 50, 40, 0.0)]                                             # без назви моделі
    for name, model, fx, fy, k1 in rows:
        conn.execute("INSERT INTO cameras (name, model, resolution_x, resolution_y, fov_x_deg, fov_y_deg, k1, "
                     "ros_namespace) VALUES (?, ?, 720, 576, ?, ?, ?, ?)", (name, model, fx, fy, k1, name.lower()))
    conn.commit()
    conn.close()


def test_migration_creates_models_from_cameras(tmp_path):
    p = tmp_path / "old.sqlite"
    _old_db(p)
    conn = db.connect(p)
    models = {m.name: m for m in cmod.list_models(conn)}
    assert set(models) == {"Caddx 256 (К1)", "Caddx 256 (К3)", "К4"}
    assert models["Caddx 256 (К1)"].cameras == 2 and models["Caddx 256 (К3)"].k1 == -0.1
    cams = {r.name: r for r in cm.list_cameras(conn)}
    assert cams["К2"].model_id == models["Caddx 256 (К1)"].id and cams["К2"].model == "Caddx 256 (К1)"
    assert cams["К3"].fov_x_deg == 25 and cams["К4"].model == "К4"


def test_save_model_updates_cameras_and_marks_stale(tmp_path):
    p = tmp_path / "old.sqlite"
    _old_db(p)
    conn = db.connect(p)
    m = next(x for x in cmod.list_models(conn) if x.cameras == 2)
    m.name, m.k1 = "Caddx Infra 256 CA", 0.05                    # дисторсія — панорами не застарівають
    mid, stale = cmod.save_model(conn, m)
    assert stale == []
    cams = {r.name: r for r in cm.list_cameras(conn)}
    assert cams["К1"].k1 == 0.05 and cams["К1"].model == "Caddx Infra 256 CA" and not cams["К1"].panorama_stale
    m.fov_x_deg = 24.0                                           # кут огляду — панорами застаріли
    mid, stale = cmod.save_model(conn, m)
    assert sorted(stale) == ["К1", "К2"]
    cams = {r.name: r for r in cm.list_cameras(conn)}
    assert cams["К1"].fov_x_deg == 24.0 and cams["К1"].panorama_stale and not cams["К3"].panorama_stale


def test_validation_and_delete(tmp_path):
    conn = db.connect(tmp_path / "p.sqlite")
    mid, _ = cmod.save_model(conn, cmod.CameraModelSpec("Тепловізор", 24, 18, sensor_x=256, sensor_y=192))
    assert cmod.get_model(conn, mid).sensor_text == "256×192"
    with pytest.raises(ValueError, match="вже є"):
        cmod.save_model(conn, cmod.CameraModelSpec("тепловізор", 30, 20))
    assert "fov_x_deg" in cmod.CameraModelSpec("X", 0, 20).validate()
    assert "sensor_x" in cmod.CameraModelSpec("X", 30, 20, sensor_x=256).validate()
    # камера з моделлю бере оптику з неї, незалежно від того, що прийшло з форми
    rec = cm.CameraRecord(name="Щогла", model_id=mid, fov_x_deg=99, latitude=50.0, longitude=30.0,
                          altitude_m=150, address="rtsp://10.0.0.5/s", stream_res_x=720, stream_res_y=576,
                          ros_namespace="mast")
    cid, _ = cm.save_camera(conn, rec, SecretBox(Fernet.generate_key()))
    got = cm.get_camera(conn, cid)
    assert got.fov_x_deg == 24 and got.fov_y_deg == 18 and got.model == "Тепловізор" and got.model_id == mid
    with pytest.raises(ValueError, match="Щогла"):
        cmod.delete_model(conn, mid)                             # модель у використанні
    mid2, _ = cmod.save_model(conn, cmod.CameraModelSpec("Зайва", 60, 34))
    cmod.delete_model(conn, mid2)
    assert [m.name for m in cmod.list_models(conn)] == ["Тепловізор"]
