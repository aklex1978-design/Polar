import sqlite3

import pytest

from polar_core import db


def test_schema_created(tmp_path):
    conn = db.connect(tmp_path / "polar.sqlite")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "cameras", "calibration", "servo_settings", "detection_mask",
            "map_levels", "targets", "detections", "events"} <= tables
    conn.close()
    # повторне відкриття не ламає базу
    assert db.connect(tmp_path / "polar.sqlite").execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_camera_cascade_and_checks():
    conn = db.connect(":memory:")
    with conn:
        conn.execute("INSERT INTO cameras (name, resolution_x, resolution_y, fov_x_deg, fov_y_deg)"
                     " VALUES ('camera_1', 1920, 1080, 60, 34)")
        cam = conn.execute("SELECT id FROM cameras").fetchone()[0]
        conn.execute("INSERT INTO servo_settings (camera_id, axis, servo_id) VALUES (?, 'pan', 1)", (cam,))
        conn.execute("INSERT INTO detection_mask (camera_id, kind, el_from) VALUES (?, 'el_min', 5)", (cam,))
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO servo_settings (camera_id, axis, servo_id) VALUES (?, 'tilt', 300)", (cam,))
    with conn:
        conn.execute("DELETE FROM cameras WHERE id = ?", (cam,))
    assert conn.execute("SELECT COUNT(*) FROM servo_settings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM detection_mask").fetchone()[0] == 0


def test_newer_schema_rejected():
    conn = sqlite3.connect(":memory:")
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError):
        db.migrate(conn)
