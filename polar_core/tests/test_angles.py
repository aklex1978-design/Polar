import pytest

from polar_core import angles as a


def test_steps():
    assert a.steps_to_deg(4096) == pytest.approx(360.0)
    assert a.deg_to_steps(90.0) == 1024
    assert a.steps_to_deg(1) == pytest.approx(0.087890625)


def test_north_offset():
    assert a.sensor_to_azimuth(350.0, 20.0) == pytest.approx(10.0)
    assert a.azimuth_to_sensor(10.0, 20.0) == pytest.approx(350.0)


def test_vectors_roundtrip():
    for az, el in [(0, 0), (90, 10), (225, 45), (359, 89)]:
        az2, el2 = a.vector_to_direction(*a.direction_vector(az, el))
        assert az2 == pytest.approx(az) and el2 == pytest.approx(el)
    assert a.direction_vector(90, 0) == pytest.approx((1.0, 0.0, 0.0), abs=1e-12)  # схід


def test_angular_distance():
    assert a.angular_distance(0, 0, 90, 0) == pytest.approx(90)
    assert a.angular_distance(10, 90, 200, 90) == pytest.approx(0, abs=1e-6)
    assert a.angular_distance(355, 0, 5, 0) == pytest.approx(10)


def test_panorama_grid():
    g = a.PanoramaGrid.from_resolution(0.1)
    assert (g.width, g.height) == (3600, 900)
    assert g.to_pixel(0, 90) == (0, 0)
    assert g.to_pixel(180, 0) == pytest.approx((1800, 900))
    assert g.to_angles(*g.to_pixel(123.4, 56.7)) == pytest.approx((123.4, 56.7))
    with pytest.raises(ValueError):
        a.PanoramaGrid(0, 10)
