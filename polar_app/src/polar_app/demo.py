"""Демонстраційна база: три камери для запуску без обладнання (--demo)."""
from __future__ import annotations

import sqlite3

DEMO_CAMERAS = [
    # назва, зміщення від півночі, простір імен, широта, довгота, адреса потоку
    ("Камера 1", 0.0, "camera_1", 50.4501, 30.5234, "rtsp://192.168.1.11:554/stream1"),
    ("Камера 2", 120.0, "camera_2", 50.4532, 30.5301, "rtsp://192.168.1.12:554/stream1"),
    ("Камера 3", 240.0, "camera_3", 50.4479, 30.5310, "rtsp://192.168.1.13:554/stream1"),
]


def seed_demo(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]:
        return
    with conn:
        for name, north, ns, lat, lon, url in DEMO_CAMERAS:
            conn.execute("INSERT INTO cameras (name, model, resolution_x, resolution_y, fov_x_deg, fov_y_deg, "
                         "north_offset_deg, ros_namespace, latitude, longitude, altitude_m, stream_url, "
                         "stream_res_x, stream_res_y) "
                         "VALUES (?, 'демо', 1920, 1080, 60, 34, ?, ?, ?, ?, 180, ?, 640, 360)",
                         (name, north, ns, lat, lon, url))
