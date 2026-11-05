import pytest

from polar_core import db


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def seed(conn, n=3):
    with conn:
        for i in range(1, n + 1):
            conn.execute("INSERT INTO cameras (name, resolution_x, resolution_y, fov_x_deg, fov_y_deg, "
                         "north_offset_deg, ros_namespace) VALUES (?, 1920, 1080, 60, 34, ?, ?)",
                         (f"Камера {i}", 30.0 * (i - 1), f"camera_{i}"))


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "polar.sqlite", check_same_thread=False)
    seed(c)
    return c
