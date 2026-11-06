import numpy as np
import pytest

from polar_core import db
from polar_core.angles import angular_distance
from polar_core.detection import (DETECTION, IDLE, TRACKING, DetectedObject, DetectionSettings, Detector,
                                  ModeMachine, Tracker, load_settings, save_settings)
from polar_core.learning import learning_plan
from polar_core.mask import UPPER, DetectionMask, MaskItem
from polar_core.panorama import Panorama
from polar_core.projection import CameraModel
from polar_core.sim import SimCamera, SimTarget, make_scene


@pytest.fixture(scope="module")
def world():
    scene = make_scene(3600, 900, seed=11)
    model = CameraModel(640, 360, 60.0, 34.0, cx=326.0, cy=176.0, k1=-0.1, tilt_deg=1.5, north_offset_deg=20.0)
    cam = SimCamera(scene, resolution=(640, 360), model=model, seed=3)
    pano = Panorama.for_camera(640, 60.0, max_px_per_deg=8.0)
    for az, el in learning_plan(60.0, 34.0):
        pano.add_frame(cam.capture(az, el), model, az, el)
    assert pano.coverage() == 1.0
    return scene, model, pano


def fresh(world, noise=2.0):
    scene, model, _pano = world
    return SimCamera(scene, resolution=(640, 360), model=model, noise_std=noise, seed=7)


def test_target_detected_with_correct_angles(world):
    """Критерій етапу 5: тестовий обʼєкт виявляється, кути — у межах критерію точності."""
    _scene, model, pano = world
    cam = fresh(world)
    cam.targets.append(SimTarget(108.0, 30.0, size_deg=1.0, brightness=10))   # кути датчика
    det = Detector(DetectionSettings())
    res = det.detect(cam.capture(100.0, 25.0), pano, model, 100.0, 25.0)
    assert res.compared and len(res.objects) == 1
    o = res.objects[0]
    assert angular_distance(o.az, o.el, 108.0 + 20.0, 30.0) < 0.5          # азимут — від півночі
    assert 0.6 < o.size_deg < 1.6


def test_no_false_positives_with_noise_light_and_sensor_error(world):
    """Шум, зміна освітлення і похибка датчика 0,4° не дають хибних спрацювань."""
    _scene, model, pano = world
    cam = fresh(world, noise=4.0)
    det = Detector(DetectionSettings())
    false = 0
    for i, (az, el) in enumerate([(10.0, 5.0), (95.0, 20.0), (200.0, 40.0), (300.0, 62.0), (45.0, 80.0)]):
        img = cam.capture(az + 0.4, el - 0.3).astype(np.float32) * (1.1 if i % 2 else 0.9) + 5
        img = np.clip(img, 0, 255).astype(np.uint8)
        res = det.detect(img, pano, model, az, el)                          # кути датчика — з похибкою
        assert res.compared
        false += len(res.objects)
    assert false == 0


def test_ecc_shift_corrects_sensor_error(world):
    _scene, model, pano = world
    cam = fresh(world)
    cam.targets.append(SimTarget(152.0, 20.0, size_deg=0.8, brightness=250))
    res = Detector(DetectionSettings()).detect(cam.capture(150.6, 18.4), pano, model, 150.0, 18.0)
    assert abs(res.aligned_shift_px[0]) > 3                                 # знайшов зсув
    o = res.objects[0]
    assert angular_distance(o.az, o.el, 172.0, 20.0) < 0.5                  # кути правильні попри похибку


def test_dead_zone_ignored_and_small_objects_filtered(world):
    _scene, model, pano = world
    cam = fresh(world)
    cam.targets.append(SimTarget(80.0, 32.0, size_deg=1.0, brightness=0))
    cam.targets.append(SimTarget(64.0, 24.0, size_deg=0.12, brightness=0))  # менший за мінімальний розмір
    mask = DetectionMask([MaskItem.rect(95, 28, 105, 36)])                  # азимут від півночі: 80 + 20
    det = Detector(DetectionSettings())
    assert det.detect(cam.capture(75.0, 28.0), pano, model, 75.0, 28.0, mask=mask).objects == []
    m2 = DetectionMask()
    m2.set_limit(UPPER, 31.0)                                               # вище 31° — мертва зона
    assert det.detect(cam.capture(75.0, 28.0), pano, model, 75.0, 28.0, mask=m2).objects == []
    assert len(det.detect(cam.capture(75.0, 28.0), pano, model, 75.0, 28.0).objects) == 1


def test_moving_camera_not_compared(world):
    _scene, model, pano = world
    res = Detector(DetectionSettings()).detect(np.zeros((360, 640), np.uint8), pano, model, 0, 0, moving=True)
    assert not res.compared and res.reason == "камера рухається"


def test_tracker_and_modes():
    tr = Tracker(gate_deg=2.0, lost_s=1.0)
    o = lambda az, el: DetectedObject(0, 0, 1, 1, 1, az, el, 1)  # noqa: E731
    _pairs, new = tr.update([o(10, 10)], 0.0)
    assert len(new) == 1
    _pairs, new = tr.update([o(10.5, 10.2)], 0.1)
    assert new == [] and tr.tracks[0].hits == 2
    _pairs, new = tr.update([o(50, 10)], 0.2)
    assert len(new) == 1 and len(tr.tracks) == 2
    tr.update([], 2.0)
    assert tr.tracks == []
    m = ModeMachine(hold_s=3.0)
    assert m.update(True, 0) == IDLE
    m.start_tracking()
    assert m.update(False, 0) == TRACKING
    assert m.update(True, 1) == DETECTION
    assert m.update(False, 2) == DETECTION and m.update(False, 3.9) == DETECTION
    assert m.update(False, 4.0) == TRACKING


def test_settings_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "p.sqlite")
    s = load_settings(conn)
    assert s == DetectionSettings()
    s.diff_threshold, s.hold_s, s.min_size_deg = 40, 2.5, 0.5
    save_settings(conn, s)
    assert load_settings(conn) == s
    from polar_core import calibration as cal
    assert cal.accuracy_criterion(conn) == s.accuracy_deg
    s.diff_threshold = 300
    with pytest.raises(ValueError):
        save_settings(conn, s)
