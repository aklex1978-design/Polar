"""Тестова база: три камери (сервоприводи — демо)."""
from __future__ import annotations

import sqlite3

DEMO_CAMERAS = [
    # назва, зміщення від півночі, простір імен, широта, довгота, протокол, адреса потоку
    # Камера 1 — перша USB-камера цього компʼютера (вебкамера): справжній потік без налаштувань.
    # Камери 2 і 3 — приклади IP-камер: вкажіть у формі адреси своїх камер.
    ("Камера 1", 0.0, "camera_1", 50.4501, 30.5234, "USB", "0"),
    ("Камера 2", 120.0, "camera_2", 50.4532, 30.5301, "RTSP", "rtsp://192.168.1.12:554/stream1"),
    ("Камера 3", 240.0, "camera_3", 50.4479, 30.5310, "RTSP", "rtsp://192.168.1.13:554/stream1"),
]


def seed_demo(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]:
        return
    with conn:
        mid = conn.execute("INSERT INTO camera_models (name, fov_x_deg, fov_y_deg, notes) "
                           "VALUES ('демо', 60, 34, 'демонстраційна модель')").lastrowid
        for name, north, ns, lat, lon, proto, url in DEMO_CAMERAS:
            conn.execute("INSERT INTO cameras (name, model, resolution_x, resolution_y, fov_x_deg, fov_y_deg, "
                         "north_offset_deg, ros_namespace, latitude, longitude, altitude_m, stream_protocol, "
                         "stream_url, stream_codec, demo_servos, model_id) "
                         "VALUES (?, 'демо', 1920, 1080, 60, 34, ?, ?, ?, ?, 180, ?, ?, ?, 1, ?)",
                         (name, north, ns, lat, lon, proto, url, "MJPEG" if proto == "USB" else "H.264", mid))
