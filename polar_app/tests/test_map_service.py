"""Етап 7: карта в застосунку — перевірка файлів, побудова рівнів у фоні, скасування, map_levels."""
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
    c.shutdown()


def test_inspect_and_center(ctx, tmp_path):
    over, det = sample_files(str(tmp_path))
    info = ctx.maps.inspect(over, need_cameras=True)
    assert info.ok and "6.90 м/піксель" in info.text and "поза межами" not in info.text
    assert "поза межами" in ctx.maps.inspect(det, need_cameras=True).text      # детальний — не всі камери
    assert "не відповідає" in ctx.maps.inspect(det, level_zoom=14).text
    bad = tmp_path / "x.tif"
    bad.write_bytes(b"not a tiff")
    assert not ctx.maps.inspect(str(bad)).ok
    c = ctx.maps.center()
    assert c == pytest.approx(((50.329 + 50.340) / 2, (28.776 + 28.800) / 2))


def test_build_sample_as_levels_z_and_z2(ctx, tmp_path):
    """Критерій етапу: прикладові файли — рівні Z і Z+2; камери в межах карти."""
    over, det = sample_files(str(tmp_path))
    ctx.maps.save_settings(mp.MapSettings(main_file=over, level_files={1: over, 3: det}))
    z, plans, errors = ctx.maps.plan()
    assert not errors and z == 14
    assert ctx.maps.start_build()
    st = ctx.maps.wait(60)
    assert st.done and not st.error, st
    lv = ctx.maps.levels()
    assert [x.state for x in lv[:3]] == ["file", "downsampled", "file"]
    assert lv[0].source_file == over and lv[2].source_file == det and lv[0].attribution == mp.OSM_ATTRIBUTION
    assert all(x.state == "upsampled" for x in lv[3:])
    assert os.path.isdir(lv[0].tiles_dir) and os.path.isdir(lv[2].tiles_dir)
    assert os.path.dirname(lv[0].tiles_dir).endswith("maps")
    for _n, la, lo in ctx.maps.camera_points():                    # камери стоять у своїх координатах
        x, y = mp.lonlat_to_merc(lo, la)
        a, b, c, d = lv[0].bounds
        assert a <= x <= c and b <= y <= d
    assert ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'map_loaded'").fetchone()[0] == 1


def test_build_errors_and_cancel(ctx, tmp_path):
    ctx.maps.save_settings(mp.MapSettings(main_file=str(tmp_path / "missing.tif")))
    assert not ctx.maps.start_build() and "не знайдено" in ctx.maps.status.error
    over, _det = sample_files(str(tmp_path))
    ctx.maps.save_settings(mp.MapSettings(main_file=over))
    ctx.maps.cancel()                                                   # скасування до старту не заважає
    assert ctx.maps.start_build()
    ctx.maps.cancel()
    st = ctx.maps.wait(60)
    assert not st.running and (st.done or "скасовано" in st.text)
