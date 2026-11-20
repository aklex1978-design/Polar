"""Рух цілі: курс, швидкість, прогноз; кути підозри однієї камери."""
import numpy as np
import pytest

from polar_core.motion import AngularTrack, MotionTrack


def test_course_speed_and_prediction():
    tr = MotionTrack(window_s=3.0)
    rng = np.random.default_rng(1)
    v = np.array([10.0, -10.0, 2.0])                     # на південний схід, набирає висоту
    for i in range(60):                                   # 6 с по 0,1 с; у вікні — 3 с
        t = i * 0.1
        tr.add(t, np.array([100.0, 200.0, 50.0]) + v * t + rng.normal(0, 0.5, 3))
    m = tr.motion()
    assert m.course_deg == pytest.approx(135.0, abs=2.0)
    assert m.speed_mps == pytest.approx(14.14, abs=0.5) and m.climb_mps == pytest.approx(2.0, abs=0.3)
    p = m.predict(10.0)                                   # через 10 с після останнього положення (t = 5,9)
    assert p == pytest.approx(np.array([100.0, 200.0, 50.0]) + v * 15.9, abs=3.0)


def test_not_enough_history_and_standing_target():
    tr = MotionTrack()
    tr.add(0.0, [0, 0, 0])
    tr.add(0.1, [0, 0, 0])
    assert tr.motion() is None                            # мало точок
    tr.add(0.1, [0, 0, 0])                                # той самий кадр ще раз — не нова точка
    tr.add(0.6, [0, 0, 0])
    m = tr.motion()
    assert m is not None and m.course_deg is None and m.speed_mps == pytest.approx(0.0)


def test_angular_motion_across_north():
    tr = AngularTrack(window_s=3.0)
    for i in range(20):                                   # 355° → 3° за 1,9 с: праворуч, через північ
        t = i * 0.1
        tr.add(t, (355.0 + 4.0 * t) % 360.0, 10.0 - 1.0 * t)
    m = tr.motion()
    assert m.az_rate == pytest.approx(4.0, abs=1e-6) and m.el_rate == pytest.approx(-1.0, abs=1e-6)
    assert m.az == pytest.approx((355.0 + 7.6) % 360.0, abs=1e-6)
    az, el = m.predict(10.0)
    assert az == pytest.approx((362.6 + 40.0) % 360.0, abs=1e-6) and el == pytest.approx(8.1 - 10.0, abs=1e-6)
    assert m.direction_text() == "праворуч і вниз"
