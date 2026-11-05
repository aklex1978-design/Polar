import math

import numpy as np
import pytest

from polar_core.projection import CameraModel, angular_error, split_wrapped
from polar_core.sim import SimCamera, SimTarget, make_scene


def model(**kw):
    base = dict(width=1920, height=1080, hfov_deg=60.0, vfov_deg=34.0)
    base.update(kw)
    return CameraModel(**base)


def test_focal_and_center():
    m = model()
    assert m.f == pytest.approx(960 / math.tan(math.radians(30)))
    assert (m.cx, m.cy) == (960, 540)
    az, el = m.pixel_to_angles(960, 540, 100.0, 20.0, north=False)
    assert az == pytest.approx(100.0) and el == pytest.approx(20.0)


def test_horizontal_edge_is_half_hfov_at_horizon():
    m = model()
    az, el = m.pixel_to_angles(1920, 540, 0.0, 0.0, north=False)
    assert az == pytest.approx(30.0) and el == pytest.approx(0.0, abs=1e-9)
    az, el = m.pixel_to_angles(960, 0, 0.0, 0.0, north=False)        # верх кадру — вище
    assert el > 0 and az == pytest.approx(0.0)


def test_matches_tz_formula_near_horizon():
    """Біля горизонту точна проєкція збігається з наближеною формулою ТЗ."""
    m = model(tilt_deg=0.0)
    u, v = 1300.0, 400.0
    az, el = m.pixel_to_angles(u, v, 50.0, 0.0, north=False)
    assert az == pytest.approx(50.0 + math.degrees(math.atan((u - 960) / m.f)), abs=0.6)
    assert el == pytest.approx(0.0 - math.degrees(math.atan((v - 540) / m.f)), abs=0.6)


@pytest.mark.parametrize("kw", [{}, dict(k1=-0.25, k2=0.08, p1=0.001, p2=-0.002, k3=-0.01),
                                dict(tilt_deg=7.0), dict(cx=1000.0, cy=500.0, tilt_deg=-4.0, k1=0.1)])
@pytest.mark.parametrize("sensor", [(0.0, 0.0), (350.0, 45.0), (123.0, 80.0)])
def test_roundtrip_pixel_angles_pixel(kw, sensor):
    m = model(north_offset_deg=37.0, **kw)
    u = np.array([0.0, 300.0, 960.0, 1500.0, 1919.0])
    v = np.array([0.0, 900.0, 540.0, 100.0, 1079.0])
    az, el = m.pixel_to_angles(u, v, *sensor)
    u2, v2, ok = m.angles_to_pixel(az, el, *sensor)
    assert ok.all()
    assert np.allclose(u2, u, atol=1e-6) and np.allclose(v2, v, atol=1e-6)


def test_behind_camera_not_visible():
    m = model()
    _u, _v, ok = m.angles_to_pixel(180.0, 0.0, 0.0, 0.0, north=False)
    assert not ok


def test_undistort_inverts_distort():
    m = model(k1=-0.3, k2=0.1, p1=0.002, p2=-0.001, k3=-0.02)
    x = np.linspace(-0.55, 0.55, 23)
    xx, yy = np.meshgrid(x, x * 0.6)
    xu, yu = m.undistort(*m.distort(xx, yy))
    assert np.allclose(xu, xx, atol=1e-9) and np.allclose(yu, yy, atol=1e-9)


def test_tilt_rotates_vertical():
    m = model(tilt_deg=10.0)
    # пряма вертикаль у світі на зображенні нахилена: точки над і під центром зміщуються в різні боки
    u_top, _v, _ = m.angles_to_pixel(0.0, 10.0, 0.0, 0.0, north=False)
    u_bot, _v, _ = m.angles_to_pixel(0.0, -10.0, 0.0, 0.0, north=False)
    assert u_top > 960 > u_bot or u_top < 960 < u_bot


def test_fov_circles():
    m = model()
    c = m.fov_circle(1.0, steps=360)
    r = np.hypot(c[:, 0] - m.cx, c[:, 1] - m.cy)
    assert np.allclose(r, 960.0)                                   # 100 % по горизонталі — край кадру
    assert CameraModel.circle_angle(0.5, 60.0) == 15.0
    md = model(k1=-0.2)
    rd = np.hypot(*(md.fov_circle(1.0)[:, :2] - [md.cx, md.cy]).T)
    assert rd.max() < 960.0                                       # бочкоподібна дисторсія стискає коло
    m2 = model(cx=1000.0, cy=600.0)
    assert np.allclose(m2.fov_circle(0.3)[:-1].mean(axis=0), [1000.0, 600.0], atol=1.0)  # коло рухається з центром


def test_frame_outline_is_wider_at_top():
    m = model()
    pts = m.frame_outline(0.0, 60.0, north=False)
    top = pts[:24]                     # верхній край
    bottom = pts[48:72]
    span = lambda p: (np.ptp(np.unwrap(np.radians(p[:, 0]))) * 180 / math.pi)  # noqa: E731
    assert span(top) > span(bottom)


def test_split_wrapped():
    pts = np.array([[350.0, 10.0], [5.0, 10.0], [5.0, 20.0], [350.0, 20.0]])
    parts = split_wrapped(pts)
    assert len(parts) == 2
    assert any(p[:, 0].min() < 0 for p in parts) or any(p[:, 0].max() > 360 for p in parts)


def test_placement_error_on_landmark_below_criterion():
    """Критерій приймання етапу 3: орієнтир, знятий з різних положень камери, з урахуванням центру,
    дисторсії й нахилу потрапляє в те саме місце панорами з похибкою, меншою за 0,5°."""
    import cv2

    truth = CameraModel(640, 360, 60.0, 34.0, cx=330.0, cy=172.0, k1=-0.18, k2=0.03, tilt_deg=3.0)
    cam = SimCamera(make_scene(1800, 450, seed=4), resolution=(640, 360), model=truth)
    cam.targets.append(SimTarget(110.0, 32.0, size_deg=1.2, brightness=0))
    found = []
    for sensor in ((100.0, 25.0), (122.0, 38.0), (95.0, 40.0)):
        img = cam.capture(*sensor)
        clear = SimCamera(cam.scene, resolution=(640, 360), model=truth).capture(*sensor)
        diff = (np.abs(img.astype(int) - clear.astype(int)) > 30).astype(np.uint8)
        m = cv2.moments(diff)
        u, v = m["m10"] / m["m00"] - 0.5, m["m01"] / m["m00"] - 0.5
        found.append(truth.pixel_to_angles(u, v, *sensor, north=False))
    errs = [angular_error(found[0], f) for f in found[1:]] + [angular_error((110.0, 32.0), f) for f in found]
    assert max(errs) < 0.5, errs
    # без калібрування (центр посередині, без дисторсії й нахилу) похибка більша за критерій
    naive = CameraModel(640, 360, 60.0, 34.0)
    assert angular_error((110.0, 32.0), naive.pixel_to_angles(*truth.angles_to_pixel(110.0, 32.0, 95.0, 40.0,
                                                                                 north=False)[:2], 95.0, 40.0,
                                                               north=False)) > 0.5
