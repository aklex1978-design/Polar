import numpy as np
import pytest

from polar_core import db
from polar_core.learning import learning_plan
from polar_core.mask import LOWER, RECT, UPPER, DetectionMask, MaskItem, load_mask, save_mask
from polar_core.panorama import Panorama
from polar_core.projection import CameraModel
from polar_core.sim import SimCamera, make_scene


def test_plan_steps_and_rows():
    plan = learning_plan(60.0, 34.0)
    azs = sorted({a for a, _ in plan})
    assert azs[1] - azs[0] == pytest.approx(30.0) and len(azs) == 12        # крок HFOV/2
    els = sorted({e for _, e in plan})
    assert els[0] == 0.0 and np.allclose(np.diff(els), 17.0)                 # крок VFOV/2
    assert els[-1] + 17.0 >= 90.0                                            # верхній ряд дістає до 90°
    assert plan[11][0] == 330.0 and plan[12][0] == 330.0                     # змійкою: ряд 2 — назад


def test_full_panorama_without_gaps():
    """Критерій етапу 4: панорама 360° × 90° без пропусків за планом Навчання, кадр лягає на сцену."""
    scene = make_scene(3600, 900, seed=5)
    model = CameraModel(320, 180, 60.0, 34.0, k1=-0.12, tilt_deg=2.0, cx=166.0, cy=88.0)
    cam = SimCamera(scene, resolution=(320, 180), model=model)
    pano = Panorama.for_camera(320, 60.0, max_px_per_deg=4.0)
    for az, el in learning_plan(60.0, 34.0):
        pano.add_frame(cam.capture(az, el), model, az, el)
    assert pano.coverage() == 1.0 and pano.gaps() == 0
    # зшита панорама збігається зі сценою (сцена 10 px/°, панорама 4 px/°)
    import cv2
    ref = cv2.resize(scene, (pano.width, pano.height), interpolation=cv2.INTER_AREA).astype(float)
    err = np.abs(pano.image[:, :, 0].astype(float) - ref)
    assert np.median(err) < 6 and np.percentile(err, 95) < 30


def test_partial_and_wrap(tmp_path):
    model = CameraModel(320, 180, 60.0, 34.0)
    cam = SimCamera(make_scene(1800, 450, seed=1), resolution=(320, 180), model=model)
    pano = Panorama(4.0)
    assert pano.coverage() == 0.0
    pano.add_frame(cam.capture(355.0, 10.0), model, 355.0, 10.0)            # через 0°/360°
    cov = pano.covered()
    assert cov[:, :10].any() and cov[:, -10:].any() and not cov[:, 400:1000].any()
    assert 0 < pano.coverage() < 0.1
    path = str(tmp_path / "pano" / "camera_1.png")
    pano.save(path)
    assert Panorama.exists(path)
    again = Panorama.load(path)
    assert again.k == 4.0 and np.array_equal(again.covered(), cov)
    assert np.array_equal(again.image, pano.image)
    with pytest.raises(ValueError):
        pano.add_frame(np.zeros((10, 10, 3), np.uint8), model, 0, 0)


def test_mask_logic():
    m = DetectionMask()
    m.set_limit(UPPER, 70)
    m.set_limit(UPPER, 75)                       # одна верхня межа
    m.set_limit(LOWER, 3)
    r = MaskItem.rect(350, 10, 20, 30, wrap=True)
    assert (r.az_from, r.az_to) == (350, 20)
    m.add_rect(r)
    assert len([i for i in m.items if i.kind == UPPER]) == 1
    assert m.is_dead(100, 80) and m.is_dead(100, 1) and not m.is_dead(100, 40)
    assert m.is_dead(5, 20) and m.is_dead(355, 20) and not m.is_dead(40, 20)
    dead = m.is_dead(np.array([5.0, 40.0]), np.array([20.0, 20.0]))
    assert dead.tolist() == [True, False]
    assert m.item_at(100, 75.5).kind == UPPER and m.item_at(0, 15) is r and m.item_at(200, 40) is None


def test_mask_save_restore(tmp_path):
    conn = db.connect(tmp_path / "p.sqlite")
    with conn:
        conn.execute("INSERT INTO cameras (name, resolution_x, resolution_y, fov_x_deg, fov_y_deg) "
                     "VALUES ('К', 640, 360, 60, 34)")
    m = DetectionMask()
    m.set_limit(UPPER, 80)
    m.add_rect(MaskItem.rect(100, 5, 120, 15))
    save_mask(conn, 1, m)
    got = load_mask(conn, 1)
    assert [(i.kind, i.el, i.az_from, i.az_to, i.el_from, i.el_to) for i in got.items] == \
        [(UPPER, 80, None, None, None, None), (RECT, None, 100, 120, 5, 15)]
    got.remove(got.rects()[0])
    save_mask(conn, 1, got)
    assert len(load_mask(conn, 1).items) == 1
