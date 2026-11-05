import pytest

from pantilt_driver.servo import ServoConfig, shortest_target, wrap180, wrap360


def test_wrap():
    assert wrap360(-10) == pytest.approx(350)
    assert wrap360(720) == 0
    assert wrap180(190) == pytest.approx(-170)
    assert wrap180(-190) == pytest.approx(170)


@pytest.mark.parametrize("current,target,expected", [
    (350.0, 10.0, 370.0),     # через 0 вперед, а не назад на 340°
    (10.0, 350.0, -10.0),     # через 0 назад
    (725.0, 0.0, 720.0),      # третій оберт
    (90.0, 180.0, 180.0),
])
def test_shortest_target(current, target, expected):
    assert shortest_target(current, target) == pytest.approx(expected)


def test_raw_axis_roundtrip():
    cfg = ServoConfig(servo_id=1, direction=-1, zero_offset_deg=100.0)
    for axis in (-30.0, 0.0, 45.5, 400.0):
        assert cfg.raw_to_axis(cfg.axis_to_raw(axis)) == pytest.approx(axis)
    assert cfg.raw_to_axis(100.0) == 0.0
    assert cfg.raw_to_axis(90.0) == pytest.approx(10.0)


def test_clamp_and_speed():
    cfg = ServoConfig(servo_id=2, min_deg=0, max_deg=90, max_speed_deg_s=120)
    assert cfg.clamp_axis(-5) == 0
    assert cfg.clamp_axis(95) == 90
    assert cfg.limit_speed(0) == 120
    assert cfg.limit_speed(500) == 120
    assert cfg.limit_speed(30) == 30


@pytest.mark.parametrize("kwargs", [
    {"servo_id": 0}, {"servo_id": 254}, {"servo_id": 1, "direction": 2},
    {"servo_id": 1, "gear_ratio": 0}, {"servo_id": 1, "max_speed_deg_s": 1000},
])
def test_config_validation(kwargs):
    with pytest.raises(ValueError):
        ServoConfig(**kwargs)
