"""Стеження за найяскравішою плямою по кадрах і плавне наведення в центр."""
import numpy as np
import pytest

from polar_core.lockon import CenterControl, LockOn
from polar_core.projection import CameraModel

MODEL = CameraModel(640, 480, 40.0, 30.0)


def frame_with(spots, bg=60):
    img = np.full((480, 640), bg, np.uint8)
    rng = np.random.default_rng(1)
    img = np.clip(img + rng.normal(0, 3, img.shape), 0, 255).astype(np.uint8)
    for u, v, r, val in spots:
        img[v - r:v + r + 1, u - r:u + r + 1] = val
    return img


def test_lock_follows_spot_while_camera_moves():
    az0, el0 = MODEL.pixel_to_angles(400, 200, 100.0, 10.0)
    lk = LockOn(az0, el0, size_px=3)
    r = lk.update(frame_with([(400, 200, 1, 230)]), MODEL, 100.0, 10.0, 0.0, k=5.0, floor=8.0)
    assert r is not None and r.u == pytest.approx(400, abs=0.5) and r.v == pytest.approx(200, abs=0.5)
    # камера повернула на 3° праворуч, ціль зсунулась на 1° — на кадрі вона лівіше
    az1, el1 = az0 + 1.0, el0
    u1, v1, _ok = MODEL.angles_to_pixel(az1, el1, 103.0, 10.0)
    r = lk.update(frame_with([(int(round(u1)), int(round(v1)), 1, 230), (100, 400, 2, 250)]), MODEL, 103.0, 10.0,
                  0.1, k=5.0, floor=8.0)
    assert r is not None and abs(r.az - az1) < 0.15 and abs(r.el - el1) < 0.15    # не інша, далека пляма
    assert lk.update(frame_with([]), MODEL, 103.0, 10.0, 0.2, k=5.0, floor=8.0) is None   # зникла


def test_center_control_is_proportional_and_has_deadband():
    c = CenterControl(deadband_deg=0.5, max_speed=60)
    servo, center = (100.0, 10.0), (100.0, 10.0)
    far = c.update(0.0, servo, center, (110.0, 10.0))
    assert far.az == pytest.approx(110.0) and far.speed == pytest.approx(15.0, rel=0.03)   # ~10° × 1,5/с
    assert c.update(0.1, servo, center, (108.0, 10.0)) is None                        # не частіше 5 разів/с
    near = c.update(0.3, (107.0, 10.0), (107.0, 10.0), (108.0, 10.5))
    assert near.speed < far.speed and near.speed >= 1.0                               # ближче — повільніше
    assert c.update(0.6, (108.0, 10.5), (108.0, 10.5), (108.2, 10.6)) is None          # у допуску — стоїть
    # центр камери не там, де сервоприводи (зсув центру, нахил): наводиться так, щоб у центр потрапила ціль
    off = CenterControl(0.5).update(0.0, (100.0, 10.0), (101.0, 9.0), (105.0, 9.0))
    assert off.az == pytest.approx(104.0) and off.el == pytest.approx(10.0)
