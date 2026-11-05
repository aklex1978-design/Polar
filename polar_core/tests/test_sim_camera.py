import numpy as np
import pytest

from polar_core.sim import SimCamera, SimTarget, make_scene


@pytest.fixture(scope="module")
def scene():
    return make_scene(1800, 450, seed=1)


def test_frame_shape_and_centre_direction(scene):
    cam = SimCamera(scene, resolution=(320, 240), fov_deg=(60, 45))
    frame = cam.capture(100.0, 30.0)
    assert frame.shape == (240, 320) and frame.dtype == np.uint8
    az, el = cam.pixel_directions(100.0, 30.0)
    assert az[120, 160] == pytest.approx(100.0, abs=0.2)
    assert el[120, 160] == pytest.approx(30.0, abs=0.2)
    assert az[120, 0] < 100.0 < az[120, -1]               # ліворуч — менший азимут
    assert el[0, 160] > 30.0 > el[-1, 160]                # вгорі — більший кут місця


def test_horizontal_fov(scene):
    cam = SimCamera(scene, resolution=(320, 240), fov_deg=(60, 45))
    az, _ = cam.pixel_directions(0.0, 0.0)
    span = (az[120, -1] - az[120, 0] + 180) % 360 - 180
    assert span == pytest.approx(60.0, abs=0.5)


def test_moving_target_changes_frame(scene):
    cam = SimCamera(scene, resolution=(320, 240), fov_deg=(60, 45))
    cam.targets.append(SimTarget(95.0, 30.0, d_az_deg_s=5.0, size_deg=2.0, brightness=0))
    f0, f1 = cam.capture(100.0, 30.0, t=0.0), cam.capture(100.0, 30.0, t=1.0)
    diff = np.abs(f0.astype(int) - f1.astype(int)) > 30
    assert diff.sum() > 50
    ys, xs = np.nonzero(diff)
    assert 100 < xs.mean() < 220 and 80 < ys.mean() < 160  # біля центру кадру


def test_static_scene_is_repeatable(scene):
    cam = SimCamera(scene, resolution=(160, 120), fov_deg=(40, 30))
    assert np.array_equal(cam.capture(10.0, 20.0), cam.capture(10.0, 20.0))
