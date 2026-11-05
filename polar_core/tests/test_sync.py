import math

import numpy as np
import pytest

from polar_core.sim import SimCamera, SimVideoCapture, make_scene
from polar_core.sync import AngleHistory, LatencyEstimator, LatencyMonitor, stamp


def test_history_interpolation_and_wrap():
    h = AngleHistory(max_gap_s=0.5)
    h.add(0.0, 350.0, 10.0, True)
    h.add(0.1, 10.0, 20.0, True)          # через 0/360
    h.add(0.2, 30.0, 30.0, False)
    a = h.at(0.05)
    assert a.azimuth_deg == pytest.approx(0.0) and a.elevation_deg == pytest.approx(15.0) and a.moving
    assert h.at(0.15).azimuth_deg == pytest.approx(20.0)
    assert h.at(0.5).azimuth_deg == pytest.approx(30.0)       # трохи після — останнє значення
    assert h.at(0.8) is None                                  # далеко — немає звʼязку
    h.add(0.15, 99.0, 99.0, False)                            # запізніле повідомлення ігнорується
    assert len(h) == 3


def test_history_gap_means_no_angles():
    h = AngleHistory(max_gap_s=0.2)
    h.add(0.0, 0.0, 0.0, False)
    h.add(2.0, 10.0, 0.0, False)
    assert h.at(1.0) is None


def test_stamp_uses_video_latency():
    h = AngleHistory()
    for i in range(11):
        h.add(i * 0.1, i * 10.0, 0.0, True)
    f = stamp(np.zeros((2, 2)), 0.5, 1, h, video_latency_ms=200)
    assert f.has_angles and f.angles.azimuth_deg == pytest.approx(30.0)


def sweep(t):
    """Повороти туди й назад з різною швидкістю, як під час вимірювання."""
    if t < 0.5:
        return 100.0
    if t < 1.5:
        return 100.0 + 40.0 * (t - 0.5)
    if t < 2.0:
        return 140.0
    if t < 2.8:
        return 140.0 - 60.0 * (t - 2.0)
    if t < 3.3:
        return 92.0
    return 92.0 + 25.0 * (t - 3.3)


@pytest.mark.parametrize("delay", [0.08, 0.25])
def test_latency_estimator_recovers_delay(delay):
    cam = SimCamera(make_scene(1800, 450, seed=3), resolution=(320, 180), fov_deg=(60, 34))
    h = AngleHistory()
    for i in range(0, 451):                          # енкодери, 50 Гц, з шумом квантування 0,088°
        t = i * 0.01
        h.add(t, round(sweep(t) / 0.088) * 0.088, 15.0, True)
    est = LatencyEstimator(hfov_deg=60, frame_width=320)
    t = 0.0
    clock = lambda: t  # noqa: E731
    cap = SimVideoCapture(cam, lambda tt: (sweep(tt), 15.0), fps=25, delay_s=delay, clock=clock, realtime=False)
    while t < 4.4:
        ok, img = cap.read()
        est.add_frame(img, t)
        t += 0.04
    lag, quality = est.estimate(h.samples())
    assert lag == pytest.approx(delay, abs=0.02)
    assert quality > 0.8


def test_latency_estimator_needs_motion():
    est = LatencyEstimator(hfov_deg=60, frame_width=64)
    img = np.random.default_rng(0).integers(0, 255, (36, 64), dtype=np.uint8)
    h = AngleHistory()
    for i in range(100):
        h.add(i * 0.02, 10.0, 0.0, False)
        est.add_frame(img, i * 0.02)
    with pytest.raises(ValueError):
        est.estimate(h.samples())


def test_latency_monitor():
    m = LatencyMonitor(limit_ms=100)
    for v in range(50):
        m.record(40 + v, has_angles=v % 10 != 0)
    assert not m.exceeded and m.frames_without_angles == 5
    for _ in range(50):
        m.record(300)
    assert m.exceeded and m.percentile() == pytest.approx(300)
