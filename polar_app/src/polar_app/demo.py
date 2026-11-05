"""Демонстраційна база: три камери для запуску без обладнання (--demo)."""
from __future__ import annotations

import sqlite3

DEMO_CAMERAS = [
    ("Камера 1", 0.0, "camera_1"),
    ("Камера 2", 120.0, "camera_2"),
    ("Камера 3", 240.0, "camera_3"),
]


def seed_demo(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]:
        return
    with conn:
        for name, north, ns in DEMO_CAMERAS:
            conn.execute("INSERT INTO cameras (name, model, resolution_x, resolution_y, fov_x_deg, fov_y_deg, "
                         "north_offset_deg, ros_namespace) VALUES (?, 'демо', 1920, 1080, 60, 34, ?, ?)",
                         (name, north, ns))
