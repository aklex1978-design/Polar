"""Наведення: вузли пошуку уздовж променя іншої камери."""
from polar_core.cueing import angular_distance, ray_search_nodes
from polar_core.geo import GeoPoint, LocalFrame, bearing_elevation


def test_ray_search_covers_true_target():
    a = GeoPoint(50.4501, 30.5234, 180.0)
    b = GeoPoint(50.4532, 30.5301, 180.0)
    frame = LocalFrame.around([a, b])
    target = GeoPoint(50.4590, 30.5260, 400.0)
    az, el, dist = bearing_elevation(frame, a, target)
    nodes = ray_search_nodes(frame, a, az, el, b, 50.0, 3000.0, step_deg=20.0)
    assert 2 <= len(nodes) <= 40
    for n1, n2 in zip(nodes, nodes[1:]):
        assert angular_distance(n1, n2) >= 20.0 - 1e-6 or n2 == nodes[-1]
    true = bearing_elevation(frame, b, target)[:2]
    # ціль між вузлами: найближчий вузол — не далі за крок (кадр B шириною ≥ крок її покриває)
    assert min(angular_distance(n, true) for n in nodes) <= 20.0


def test_ray_search_respects_allowed():
    a = GeoPoint(50.4501, 30.5234, 180.0)
    b = GeoPoint(50.4532, 30.5301, 180.0)
    frame = LocalFrame.around([a, b])
    nodes = ray_search_nodes(frame, a, 10.0, 10.0, b, 50.0, 3000.0, 10.0, allowed=lambda az, el: el <= 15.0)
    assert nodes and all(el <= 15.0 for _az, el in nodes)
    assert ray_search_nodes(frame, a, 10.0, 10.0, b, 50.0, 3000.0, 10.0, allowed=lambda az, el: False) == []
