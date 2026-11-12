"""Етап 7: карта в застосунку — на кожному рівні своя карта; завантаження у фоні, скасування, map_levels."""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "polar_core", "tests"))

from geotiff_util import area_raster, write_geotiff  # noqa: E402

from polar_app.__main__ import build_context  # noqa: E402
from polar_core import db  # noqa: E402
from polar_core import maps as mp  # noqa: E402

import fakes  # noqa: E402
from demo_data import seed_demo  # noqa: E402

CAMS = [(50.331, 28.78), (50.340, 28.800), (50.329, 28.776)]


def no_ros():
    raise ImportError("No module named 'rclpy'")


def sample_files(d):
    r = 6.9 / math.cos(math.radians(50.34))
    over, ox, oy = area_raster(50.345, 28.75, 50.318, 28.81, r, seed=1)
    det, dx, dy = area_raster(50.336, 28.77, 50.326, 28.79, r / 4, seed=2)
    return (write_geotiff(os.path.join(d, "Вереси_118014.tiff"), over, ox, oy, r,
                          description="Rendered from OpenStreetMap data"),
            write_geotiff(os.path.join(d, "Вереси_1.tiff"), det, dx, dy, r / 4))


@pytest.fixture
def ctx(tmp_path):
    path = str(tmp_path / "polar.sqlite")
    conn = db.connect(path)
    seed_demo(conn)
    with conn:
        for i, (la, lo) in enumerate(CAMS, start=1):
            conn.execute("UPDATE cameras SET latitude = ?, longitude = ? WHERE id = ?", (la, lo, i))
    c = fakes.bind(build_context(path, video_options=fakes.video_options(), ros_factory=no_ros))
    yield c
    c.maps.shutdown()
    c.shutdown()


def test_inspect_and_center(ctx, tmp_path):
    over, det = sample_files(str(tmp_path))
    info = ctx.maps.inspect(over)
    assert info.ok and "6.90 м/піксель" in info.text and "поза картою" not in info.text
    assert "поза картою" in ctx.maps.inspect(det).text                  # детальний — не всі камери
    bad = tmp_path / "x.tif"
    bad.write_bytes(b"not a tiff")
    assert not ctx.maps.inspect(str(bad)).ok
    assert "не знайдено" in ctx.maps.inspect(str(tmp_path / "missing.tif")).text
    assert ctx.maps.center() == pytest.approx(((50.329 + 50.340) / 2, (28.776 + 28.800) / 2))


def test_each_level_loads_own_map(ctx, tmp_path):
    """Кожен рівень — своя карта у власному масштабі; прикладові файли — два рівні, що різняться в 4 рази."""
    over, det = sample_files(str(tmp_path))
    ctx.maps.load_level(2, over)
    ctx.maps.load_level(5, det)
    ctx.maps.wait(60)
    assert ctx.maps.status[2].text == "завантажено" and ctx.maps.status[5].text == "завантажено"
    lv = {x.level: x for x in ctx.maps.levels()}
    assert set(lv) == {2, 5}
    assert lv[2].source_file == over and lv[5].source_file == det
    assert lv[2].res / lv[5].res == pytest.approx(4.0, rel=1e-6)
    assert lv[2].attribution == mp.OSM_ATTRIBUTION and ctx.maps.settings().osm      # визначено з файлу
    assert "поза картою" in lv[5].warning and not lv[2].warning
    assert os.path.isdir(lv[2].tiles_dir) and os.path.dirname(lv[2].tiles_dir).endswith("maps")
    for _n, la, lo in ctx.maps.camera_points():                      # камери стоять у межах оглядової
        x, y = mp.lonlat_to_merc(lo, la)
        a, b, c, d = lv[2].bounds
        assert a <= x <= c and b <= y <= d
    assert ctx.maps.settings().level_files == {2: over, 5: det}
    ctx.maps.remove_level(5)
    assert {x.level for x in ctx.maps.levels()} == {2} and not os.path.isdir(lv[5].tiles_dir)
    assert ctx.maps.settings().level_files == {2: over}
    assert ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'map_loaded'").fetchone()[0] == 2


def test_bad_file_and_cancel(ctx, tmp_path):
    bad = tmp_path / "x.tif"
    bad.write_bytes(b"not a tiff")
    ctx.maps.load_level(1, str(bad))
    ctx.maps.wait(30)
    assert ctx.maps.status[1].error and not ctx.maps.levels()
    over, _det = sample_files(str(tmp_path))
    ctx.maps.load_level(1, over)
    ctx.maps.wait(30)
    first = ctx.maps.level(1)
    assert first is not None
    ctx.maps.load_level(1, over)
    ctx.maps.cancel(1)
    ctx.maps.wait(30)
    assert not ctx.maps.busy() and ctx.maps.level(1) is not None          # попередня карта рівня лишилась
