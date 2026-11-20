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
    cam.targets.append(SimTarget(108.0, 30.0, size_deg=1.0, brightness=255))   # кути датчика
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
    cam.targets.append(SimTarget(80.0, 32.0, size_deg=1.0, brightness=255))
    mask = DetectionMask([MaskItem.rect(95, 28, 105, 36)])                  # азимут від півночі: 80 + 20
    det = Detector(DetectionSettings())
    assert det.detect(cam.capture(75.0, 28.0), pano, model, 75.0, 28.0, mask=mask).objects == []
    m2 = DetectionMask()
    m2.set_limit(UPPER, 31.0)                                               # вище 31° — мертва зона
    assert det.detect(cam.capture(75.0, 28.0), pano, model, 75.0, 28.0, mask=m2).objects == []
    assert len(det.detect(cam.capture(75.0, 28.0), pano, model, 75.0, 28.0).objects) == 1


def _blank(world, az=75.0, el=28.0):
    _scene, model, _pano = world
    return fresh(world, noise=0.0).capture(az, el)


def test_object_size_shape_and_brightness_rules(world):
    """Обʼєкт — суцільна яскрава пляма площею понад 1 піксель будь-якої форми; 1 піксель, темна пляма і
    пляма з дірою (кільце) — не обʼєкти."""
    _scene, model, pano = world
    det = Detector(DetectionSettings())

    def objects(paint):
        img = _blank(world).astype(np.int16)
        paint(img)
        return det.detect(np.clip(img, 0, 255).astype(np.uint8), pano, model, 75.0, 28.0).objects

    def put(img, pts, value=255):
        for y, x in pts:
            img[y, x] = value

    assert objects(lambda im: put(im, [(100, 200)])) == []                               # 1 піксель — шум
    two = objects(lambda im: put(im, [(100, 200), (100, 201)]))                           # 2 пікселі — обʼєкт
    assert len(two) == 1 and two[0].area_px == 2
    assert len(objects(lambda im: put(im, [(100, 200), (101, 201)]))) == 1                # по діагоналі — суцільна
    assert len(objects(lambda im: put(im, [(y, 300 + y - 80) for y in range(80, 95)]))) == 1   # довга смуга
    l_shape = [(150, x) for x in range(400, 410)] + [(y, 400) for y in range(150, 160)]
    assert len(objects(lambda im: put(im, l_shape))) == 1                                 # будь-яка форма
    assert objects(lambda im: put(im, [(100, 200), (100, 201)], value=0)) == []          # темна — не ціль
    ring = [(y, x) for y in range(60, 72) for x in range(500, 512)
            if not (63 <= y <= 68 and 503 <= x <= 508)]
    assert objects(lambda im: put(im, ring)) == []                                        # з дірою — не суцільна
    assert len(objects(lambda im: put(im, [(y, x) for y in range(60, 72) for x in range(500, 512)]))) == 1


def test_tracker_requires_consecutive_frames():
    """Обʼєкт стає ціллю, лише коли є щонайменше на 2 кадрах поспіль; одиночні спалахи — ні."""
    tr = Tracker(gate_deg=2.0, lost_s=1.0, min_frames=2)
    o = lambda az, el: DetectedObject(0, 0, 1, 1, 2, az, el, 0.1)  # noqa: E731
    assert tr.update([o(10, 10)], 0.00) == ([], [])                  # перший кадр — ще не ціль
    assert tr.update([], 0.04) == ([], [])                           # зник — серію перервано
    assert tr.update([o(10, 10)], 0.08) == ([], [])                  # знову перший кадр серії
    pairs, new = tr.update([o(10.1, 10)], 0.12)                      # другий поспіль — ціль
    assert len(pairs) == 1 and new == [pairs[0][0]] and pairs[0][0].confirmed
    pairs, new = tr.update([o(10.2, 10), o(40, 5)], 0.16)            # нова пляма — ще не ціль
    assert len(pairs) == 1 and new == []
    pairs, new = tr.update([], 0.20)                                 # підтверджена не зникає через 1 кадр
    assert pairs == [] and tr.tracks[0].confirmed


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
    s.sensitivity_k, s.hold_s, s.min_area_px, s.min_frames = 4.0, 2.5, 3, 4
    save_settings(conn, s)
    assert load_settings(conn) == s
    from polar_core import calibration as cal
    assert cal.accuracy_criterion(conn) == s.accuracy_deg
    s.min_threshold = 300
    with pytest.raises(ValueError):
        save_settings(conn, s)


def test_only_brightest_objects_at_most_five(world):
    """Не більше 5 обʼєктів — найяскравіші, найяскравіший перший."""
    _scene, model, pano = world
    img = _blank(world).astype(np.int16)
    levels = [60, 75, 90, 105, 120, 135, 150]                      # над небом ~180: 7 плям різної яскравості
    for k, add in enumerate(levels):
        y, x = 60, 60 + k * 70
        img[y:y + 2, x:x + 2] += add
    frame = np.clip(img, 0, 255).astype(np.uint8)
    every = Detector(DetectionSettings(max_objects=100)).detect(frame, pano, model, 75.0, 28.0).objects
    assert len(every) == 7
    res = Detector(DetectionSettings()).detect(frame, pano, model, 75.0, 28.0)
    assert len(res.objects) == 5
    b = [o.brightness for o in res.objects]
    assert b == sorted(b, reverse=True)
    assert b == sorted((o.brightness for o in every), reverse=True)[:5]          # саме 5 найяскравіших


def test_noise_threshold_per_tile():
    from polar_core.detection import noise_threshold
    rng = np.random.default_rng(0)
    d = rng.normal(0, 2.0, (240, 320)).astype(np.float32)
    d[:, 160:] = rng.normal(0, 10.0, (240, 160))                  # права половина — «марево», шум у 5 разів більший
    d[100:102, 50:52] = 200                                       # поодинокі цілі на шум майже не впливають
    noise, thr = noise_threshold(d, np.ones_like(d, bool), k=5.0, floor=8.0)
    assert noise[:, :3].mean() == pytest.approx(2.0, rel=0.2) and noise[:, 5:].mean() == pytest.approx(10.0, rel=0.2)
    assert thr[120, 20] == pytest.approx(10.0, rel=0.25) and thr[120, 300] == pytest.approx(50.0, rel=0.2)
    _n, flat = noise_threshold(np.zeros((64, 64), np.float32), np.ones((64, 64), bool), 5.0, 8.0)
    assert flat.min() == pytest.approx(8.0)                       # рівний кадр — мінімальний поріг


def test_relative_threshold_sees_faint_target_on_clean_sky_but_not_noise(world):
    """Чисте небо: слабка ціль (+20) видна — фіксований поріг 30 її б пропустив. Шумний кадр (σ 8):
    поріг піднімається сам — хибних тривог немає."""
    _scene, model, pano = world
    det = Detector(DetectionSettings())
    clean = _blank(world).astype(np.int16)
    clean[100:102, 200:202] += 20
    res = det.detect(np.clip(clean, 0, 255).astype(np.uint8), pano, model, 75.0, 28.0)
    assert len(res.objects) == 1 and res.threshold < 20
    noisy = (_blank(world) + np.random.default_rng(5).normal(0, 8.0, clean.shape)).clip(0, 255).astype(np.uint8)
    res = det.detect(noisy, pano, model, 75.0, 28.0)
    assert res.objects == [] and res.threshold > 30
