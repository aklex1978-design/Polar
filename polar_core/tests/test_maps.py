"""Етап 7: карта — проєкція, GeoTIFF, 8 рівнів, тайли, map_levels."""
import math
import os

import cv2
import numpy as np
import pytest

from polar_core import db
from polar_core import imageio
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


def test_build_level_in_own_scale(sample, tmp_path):
    """Рівень = власний файл у власному масштабі: тайли в сітці пікселів файлу, мітки — на місці."""
    for path, color in ((sample[0], RED), (sample[1], BLUE)):
        r = mp.read_geotiff(path)
        calls = []
        out = str(tmp_path / "maps" / os.path.basename(path))
        lv = mp.build_level(r, 1, out, mp.OSM_ATTRIBUTION, progress=lambda f, t: calls.append(f) or True)
        assert calls and calls[-1] == pytest.approx(1.0)
        assert lv.res == pytest.approx(r.res) and lv.bounds == pytest.approx(r.bounds)
        x, y = mp.lonlat_to_merc(CAM[1], CAM[0])
        px, py = (x - lv.x0) / lv.res, (lv.y0 - y) / lv.res
        tile = imageio.imread(lv.tile_path(int(px // 256), int(py // 256)), cv2.IMREAD_UNCHANGED)
        v = tile[int(py % 256), int(px % 256)]
        assert tuple(int(c) for c in v[2::-1]) == color and v[3] == 255       # BGRA → RGB
    # прикладові файли: детальний у 4 рази дрібніший — два рівні зуму вебкарти
    a, b = mp.read_geotiff(sample[0]), mp.read_geotiff(sample[1])
    assert b.native_zoom - a.native_zoom == pytest.approx(2.0, abs=0.01)


def test_pyramid_copies(tmp_path):
    """Зменшені вдвічі копії (INTER_AREA) — щоб уся карта у вигляді була чіткою, як у переглядачі."""
    img = np.zeros((1000, 1500, 4), np.uint8)
    img[..., 3] = 255
    img[:, :750, :3] = 255                                   # ліва половина біла
    p = write_geotiff(str(tmp_path / "a4.tif"), img, 3.0e6, 6.5e6, 100.0)
    lv = mp.build_level(mp.read_geotiff(p), 7, str(tmp_path / "maps" / "7"))
    assert mp.pyramid_depth(1500, 1000) == 3 and lv.pyramid() == 3
    t = imageio.imread(lv.tile_path(0, 0, 3), cv2.IMREAD_UNCHANGED)        # 1500/8 ≈ 188 × 125 — один тайл
    assert t.shape == (256, 256, 4) and t[10, 10, 3] == 255 and t[200, 200, 3] == 0
    assert t[10, 50, 0] == 255 and t[10, 150, 0] == 0                  # межа половин — на місці (≈ 94)


def test_cancel_keeps_previous_level(sample, tmp_path):
    r = mp.read_geotiff(sample[0])
    out = str(tmp_path / "maps" / "1")
    mp.build_level(r, 1, out)
    before = sorted(os.listdir(out))
    with pytest.raises(mp.Cancelled):
        mp.build_level(r, 1, out, progress=lambda f, t: False)
    assert sorted(os.listdir(out)) == before and not os.path.exists(out + ".building")


def test_levels_and_settings_in_db(sample, tmp_path):
    conn = db.connect(tmp_path / "p.sqlite")
    over, det = mp.read_geotiff(sample[0]), mp.read_geotiff(sample[1])
    l1 = mp.build_level(over, 1, str(tmp_path / "maps" / "1"))
    l2 = mp.build_level(det, 2, str(tmp_path / "maps" / "2"))
    mp.save_level(conn, l2)
    mp.save_level(conn, l1)
    mp.set_attribution(conn, mp.OSM_ATTRIBUTION)
    lv = mp.load_levels(conn)
    assert [x.level for x in lv] == [1, 2]
    assert lv[0].source_file == over.path and lv[0].width == over.width and lv[0].height == over.height
    assert lv[1].res == pytest.approx(det.res) and lv[1].bounds == pytest.approx(det.bounds)
    assert lv[0].attribution == mp.OSM_ATTRIBUTION and lv[0].zoom == pytest.approx(13.8, abs=0.05)
    mp.delete_level(conn, 1)
    assert [x.level for x in mp.load_levels(conn)] == [2]
    s = mp.MapSettings(level_files={1: over.path, 3: det.path}, osm=True)
    mp.save_map_settings(conn, s)
    s2 = mp.load_map_settings(conn)
    assert s2.level_files == {1: over.path, 3: det.path} and s2.osm


def test_unicode_paths(tmp_path):
    """Шляхи з кирилицею (на Windows cv2.imwrite/imread з ними не працюють)."""
    d = tmp_path / "Документи" / "Вереси"
    d.mkdir(parents=True)
    img = np.zeros((20, 30, 4), np.uint8)
    img[5, 7] = (1, 2, 3, 255)
    p = str(d / "тайл.png")
    assert imageio.imwrite(p, img)
    back = imageio.imread(p, cv2.IMREAD_UNCHANGED)
    assert back.shape == (20, 30, 4) and tuple(back[5, 7]) == (1, 2, 3, 255)
    assert imageio.imread(str(d / "немає.png")) is None
