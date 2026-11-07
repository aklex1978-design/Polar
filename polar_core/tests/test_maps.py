"""Етап 7: карта — проєкція, GeoTIFF, 8 рівнів, тайли, map_levels."""
import math
import os

import cv2
import numpy as np
import pytest

from polar_core import db
from polar_core import maps as mp

from geotiff_util import area_raster, write_geotiff

# Вереси (Житомирщина): камера всередині обох файлів
CAM = (50.331, 28.78)
RED, BLUE = (255, 0, 0), (0, 0, 255)


def test_projection():
    assert mp.res_at_zoom(0) == pytest.approx(156543.0339, rel=1e-9)
    x, y = mp.lonlat_to_merc(28.78, 50.331)
    lon, lat = mp.merc_to_lonlat(x, y)
    assert lon == pytest.approx(28.78, abs=1e-9) and lat == pytest.approx(50.331, abs=1e-9)
    # прикладовий оглядовий файл: ≈ 6,9 м/піксель на місцевості ↔ зум ≈ 13,8
    res = 6.9 / math.cos(math.radians(50.34))
    assert mp.zoom_for_res(res) == pytest.approx(13.8, abs=0.05)
    assert mp.map_center([(50.0, 28.0), (50.4, 28.6), (50.2, 28.1)]) == pytest.approx((50.2, 28.3))
    assert mp.map_center([(50.3, 28.7)]) == (50.3, 28.7)


@pytest.fixture
def sample(tmp_path):
    """Як у ТЗ: оглядовий (≈ 6,9 м/пікс., зум 13,8) і детальний у 4 рази дрібніший (зум 15,8) всередині."""
    r_over = 6.9 / math.cos(math.radians(50.34))
    over, ox, oy = area_raster(50.345, 28.75, 50.318, 28.81, r_over, seed=1, marks=[(CAM[0], CAM[1], RED)])
    det, dx, dy = area_raster(50.336, 28.77, 50.326, 28.79, r_over / 4, seed=2, marks=[(CAM[0], CAM[1], BLUE)])
    p_over = write_geotiff(str(tmp_path / "Вереси_118014.tiff"), over, ox, oy, r_over,
                           description="Rendered from OpenStreetMap data")
    p_det = write_geotiff(str(tmp_path / "Вереси_1.tiff"), det, dx, dy, r_over / 4)
    return p_over, p_det


def test_read_geotiff_lzw_3857(sample):
    r = mp.read_geotiff(sample[0])
    assert r.epsg == 3857 and r.image.shape[2] == 4 and r.osm
    assert r.ground_res == pytest.approx(6.9, rel=0.01)
    assert r.native_zoom == pytest.approx(13.8, abs=0.05)
    assert r.contains(*CAM) and not r.contains(50.5, 28.78)
    lo0, la0, lo1, la1 = r.lonlat_bounds()
    assert la1 == pytest.approx(50.345, abs=1e-3) and lo0 == pytest.approx(28.75, abs=1e-3)
    d = mp.read_geotiff(sample[1])
    assert d.native_zoom == pytest.approx(15.8, abs=0.05) and not d.osm


def test_reject_without_georef(tmp_path):
    img = np.zeros((10, 10, 4), np.uint8)
    p = write_geotiff(str(tmp_path / "plain.tif"), img, 0, 0, 1, georef=False)
    with pytest.raises(mp.GeoTiffError, match="привʼязки"):
        mp.read_geotiff(p)


def test_geographic_file_reprojected(tmp_path):
    # файл у широті/довготі (EPSG:4326) з міткою в точці камери
    lat0, lon0, d = 50.345, 28.75, 0.0002
    h, w = 150, 300
    img = np.zeros((h, w, 4), np.uint8)
    img[..., 3] = 255
    r, c = int((lat0 - CAM[0]) / d), int((CAM[1] - lon0) / d)
    img[r - 2:r + 3, c - 2:c + 3, :3] = RED
    p = write_geotiff(str(tmp_path / "wgs84.tif"), img, lon0, lat0, d, epsg=4326, geographic=True)
    g = mp.read_geotiff(p)
    assert g.epsg == 4326
    x, y = mp.lonlat_to_merc(CAM[1], CAM[0])
    cc, rr = int((x - g.x0) / g.res_x), int((g.y0 - y) / g.res_y)
    assert tuple(g.image[rr, cc, :3]) == RED                   # мітка — у своїх координатах після перерахунку


def test_plan_sample_files_as_levels_z_and_z2(sample):
    """Критерій етапу: прикладові файли завантажуються як рівні Z і Z+2."""
    over, det = mp.read_geotiff(sample[0]), mp.read_geotiff(sample[1])
    z = mp.default_base_zoom([over, det])
    assert z == 14
    plans = mp.plan_levels(z, over, {3: det})
    st = {p.level: p.state for p in plans}
    assert st[1] == "file" and plans[0].source == over.path
    assert st[2] == "downsampled" and plans[1].source == det.path          # з найближчого детальнішого
    assert st[3] == "file" and plans[2].source == det.path and not plans[2].warning
    assert all(st[k] == "upsampled" for k in range(4, 9)) and plans[7].source_level == 3
    assert plans[0].zoom == 14 and plans[7].zoom == 21
    # файл, що не відповідає рівню, — попередження
    bad = mp.plan_levels(z, over, {5: det})
    assert "не відповідає" in bad[4].warning


def test_build_tiles_marks_in_place(sample, tmp_path):
    over, det = mp.read_geotiff(sample[0]), mp.read_geotiff(sample[1])
    plans = mp.plan_levels(14, over, {3: det})
    rasters = {over.path: over, det.path: det}
    calls = []
    out = str(tmp_path / "maps")
    mp.build_levels(plans, rasters, out, progress=lambda f, t: calls.append(f) or True)
    assert calls and calls[-1] == pytest.approx(1.0)
    x, y = mp.lonlat_to_merc(CAM[1], CAM[0])
    for level, color in ((1, RED), (3, BLUE)):
        zoom = 14 + level - 1
        gx, gy = mp.global_px(x, y, zoom)
        tile = cv2.imread(os.path.join(out, str(level), str(int(gx // 256)), f"{int(gy // 256)}.png"),
                          cv2.IMREAD_UNCHANGED)
        px = tile[int(gy % 256), int(gx % 256)]
        assert tuple(int(v) for v in px[2::-1]) == color and px[3] == 255      # BGRA → RGB
    assert not os.path.isdir(os.path.join(out, "4"))                            # збільшені — не на диску
    assert mp.estimate_bytes(plans) > 0


def test_cancel_keeps_previous_map(sample, tmp_path):
    over = mp.read_geotiff(sample[0])
    plans = mp.plan_levels(14, over, {})
    out = str(tmp_path / "maps")
    mp.build_levels(plans, {over.path: over}, out)
    before = sorted(os.listdir(out))
    with pytest.raises(mp.Cancelled):
        mp.build_levels(plans, {over.path: over}, out, progress=lambda f, t: False)
    assert sorted(os.listdir(out)) == before and not os.path.exists(out + ".building")


def test_levels_and_settings_in_db(sample, tmp_path):
    conn = db.connect(tmp_path / "p.sqlite")
    over, det = mp.read_geotiff(sample[0]), mp.read_geotiff(sample[1])
    plans = mp.plan_levels(14, over, {3: det})
    mp.save_levels(conn, plans, str(tmp_path / "maps"), mp.OSM_ATTRIBUTION)
    lv = mp.load_levels(conn)
    assert [x.level for x in lv] == list(range(1, 9))
    assert lv[2].state == "file" and lv[2].source_file == det.path and lv[2].zoom == 16
    assert lv[5].state == "upsampled" and lv[5].source_level == 3 and lv[0].attribution == mp.OSM_ATTRIBUTION
    assert lv[0].bounds == pytest.approx(over.bounds)
    s = mp.MapSettings(main_file=over.path, base_zoom=14, level_files={3: det.path})
    mp.save_map_settings(conn, s)
    s2 = mp.load_map_settings(conn)
    assert s2.main_file == over.path and s2.base_zoom == 14 and s2.level_files == {3: det.path}
