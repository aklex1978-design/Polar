"""Етап 6: промені, тріангуляція, правило Б, три і більше камер."""
import math
import random

import numpy as np
import pytest

from polar_core.geo import GeoPoint, LocalFrame, bearing_elevation
from polar_core.triangulation import (Confirmation, MatchParams, check_pair, make_ray, solve)

# камери на місцевості (Київ), висоти над рівнем моря
CAMS = {
    "Камера 1": GeoPoint(50.45000, 30.52000, 180.0),
    "Камера 2": GeoPoint(50.45000, 30.52700, 175.0),      # ≈ 500 м на схід
    "Камера 3": GeoPoint(50.45400, 30.52350, 190.0),      # ≈ 450 м на північ
}
FRAME = LocalFrame.around(CAMS.values())
P = MatchParams(accuracy_deg=0.5, k=2.0, sync_window_s=0.04, min_angle_deg=5.0, max_range_m=3000.0)


def rays_to(target: GeoPoint, cams=CAMS, noise_deg=0.0, rng=None, obj=0, t=0.0):
    out = []
    for name, pos in cams.items():
        az, el, _d = bearing_elevation(FRAME, pos, target)
        if noise_deg:
            az += rng.gauss(0, noise_deg)
            el += rng.gauss(0, noise_deg)
        out.append(make_ray(FRAME, pos, name, obj, az, el, t))
    return out


def geo_error_m(a: GeoPoint, b: GeoPoint) -> float:
    return float(np.linalg.norm(FRAME.to_enu(a) - FRAME.to_enu(b)))


def test_geo_roundtrip_and_bearing():
    p = GeoPoint(50.4561, 30.5123, 312.5)
    back = FRAME.to_geo(FRAME.to_enu(p))
    assert geo_error_m(p, back) < 1e-3
    assert back.altitude_m == pytest.approx(312.5, abs=1e-3)
    az, el, d = bearing_elevation(FRAME, CAMS["Камера 1"], GeoPoint(50.45, 30.53, 180.0))
    assert az == pytest.approx(90.0, abs=0.01) and abs(el) < 0.1 and d == pytest.approx(711, rel=0.01)


def test_two_cameras_exact_angles_give_gps():
    target = GeoPoint(50.4530, 30.5240, 330.0)                   # ≈ 300 м від камер, 150 м над ними
    t = solve(rays_to(target, {k: CAMS[k] for k in ("Камера 1", "Камера 2")}), FRAME, P)
    assert len(t) == 1 and sorted(t[0].cameras) == ["Камера 1", "Камера 2"]
    assert geo_error_m(t[0].geo, target) < 0.05
    assert t[0].geo.altitude_m == pytest.approx(330.0, abs=0.05)
    for cam in ("Камера 1", "Камера 2"):                          # Dᵢ — власна для кожної камери
        assert t[0].distances[cam] == pytest.approx(bearing_elevation(FRAME, CAMS[cam], target)[2], abs=0.05)


def test_acceptance_test_target_within_tolerance():
    """Критерій етапу: координати тестової цілі збігаються з її GPS у межах допуску k · D · tg δ
    за реального шуму кутів (σ = 0,1°, похибка розміщення кадру в межах δ) — для цілей у межах
    бази камер (кут між променями великий)."""
    rng = random.Random(7)
    for i in range(200):
        target = GeoPoint(50.4515 + rng.uniform(-0.004, 0.004), 30.5235 + rng.uniform(-0.005, 0.005),
                          rng.uniform(200, 600))
        res = solve(rays_to(target, noise_deg=0.1, rng=rng), FRAME, P)
        assert len(res) == 1, i
        tgt = res[0]
        assert geo_error_m(tgt.geo, target) <= P.tolerance(tgt.distance_m), (i, tgt.cameras)


def test_far_targets_error_grows_with_small_ray_angle():
    """Далі за базу камер похибка вздовж променя зростає як 1 / sin γ (γ — найбільший кут між
    променями): правило Б обмежує бокове розходження, а не дальність. Межа — k · D · tg δ / sin γ."""
    rng = random.Random(5)
    for i in range(200):
        target = GeoPoint(50.4500 + rng.uniform(-0.012, 0.015), 30.5235 + rng.uniform(-0.018, 0.018),
                          rng.uniform(200, 600))
        res = solve(rays_to(target, noise_deg=0.1, rng=rng), FRAME, P)
        assert len(res) == 1, i
        tgt = res[0]
        assert geo_error_m(tgt.geo, target) <= P.tolerance(tgt.distance_m) / math.sin(
            math.radians(tgt.max_angle_deg)), (i, tgt.max_angle_deg)


def test_kilometre_scale_uses_each_camera_vertical():
    """На кілометрах вертикаль камер різна (≈ 0,03° на 3 км): напрям переводиться точно."""
    far = {"A": GeoPoint(50.40, 30.40, 150.0), "B": GeoPoint(50.40, 30.445, 150.0)}   # ≈ 3,2 км
    target = GeoPoint(50.42, 30.42, 900.0)
    t = solve(rays_to(target, far), FRAME, MatchParams(max_range_m=10000))
    assert len(t) == 1 and geo_error_m(t[0].geo, target) < 0.05


def test_rule_b_conditions():
    c1, c2 = CAMS["Камера 1"], CAMS["Камера 2"]
    target = GeoPoint(50.4530, 30.5240, 330.0)
    a1, e1, _ = bearing_elevation(FRAME, c1, target)
    a2, e2, _ = bearing_elevation(FRAME, c2, target)
    r1 = make_ray(FRAME, c1, "1", 0, a1, e1)
    assert check_pair(r1, make_ray(FRAME, c2, "2", 0, a2, e2), P).ok
    # ціль позаду камери: промінь 2 дивиться в протилежний бік
    assert "позаду" in check_pair(r1, make_ray(FRAME, c2, "2", 0, (a2 + 180) % 360, -e2), P).reason
    # кадри не синхронні
    assert "синхронізації" in check_pair(r1, make_ray(FRAME, c2, "2", 0, a2, e2, t=0.1), P).reason
    # розходження: камера 2 «бачить» на 2° вище — на 300 м це ≈ 10 м > допуску ≈ 5 м
    bad = check_pair(r1, make_ray(FRAME, c2, "2", 0, a2, e2 + 2.0), P)
    assert not bad.ok and "допуск" in bad.reason and bad.miss_m > bad.tolerance_m
    # майже паралельні промені — далека ціль на лінії, де кут < 5°
    far = GeoPoint(50.38, 30.5235, 200.0)                       # ≈ 7,8 км на південь
    fa1, fe1, _ = bearing_elevation(FRAME, c1, far)
    fa2, fe2, _ = bearing_elevation(FRAME, c2, far)
    pc = check_pair(make_ray(FRAME, c1, "1", 0, fa1, fe1), make_ray(FRAME, c2, "2", 0, fa2, fe2), P)
    assert not pc.ok and "кут" in pc.reason
    # далі за максимальну дальність
    pc = check_pair(make_ray(FRAME, c1, "1", 0, fa1, fe1), make_ray(FRAME, c2, "2", 0, fa2, fe2),
                    MatchParams(min_angle_deg=1.0, max_range_m=3000))
    assert not pc.ok and "дальн" in pc.reason


def test_tolerance_example_from_spec():
    """ТЗ: при δ = 0,5° і k = 2 допуск ≈ 1,7 м на 100 м і ≈ 35 м на 2 км."""
    assert P.tolerance(100) == pytest.approx(1.75, abs=0.05)
    assert P.tolerance(2000) == pytest.approx(34.9, abs=0.2)


def test_several_targets_each_object_in_one_pair():
    """Дві цілі на двох камерах: правильні пари, «привиди» перехресних пар відкидаються."""
    t_a = GeoPoint(50.4530, 30.5220, 260.0)
    t_b = GeoPoint(50.4520, 30.5255, 420.0)
    two = {k: CAMS[k] for k in ("Камера 1", "Камера 2")}
    rays = rays_to(t_a, two, obj="a") + rays_to(t_b, two, obj="b")
    res = solve(rays, FRAME, P)
    assert len(res) == 2
    for t in res:
        objs = {r.obj for r in t.rays}
        assert len(objs) == 1                               # обидва промені — від однієї цілі
        assert geo_error_m(t.geo, t_a if objs == {"a"} else t_b) < 0.05
    keys = [r.key for t in res for r in t.rays]
    assert len(keys) == len(set(keys))                      # кожен обʼєкт камери — в одній цілі


def test_three_cameras_and_outlier():
    """Три камери: T — найменші квадрати за всіма трьома; хибний промінь четвертої камери відкидається."""
    rng = random.Random(3)
    target = GeoPoint(50.4525, 30.5235, 400.0)
    rays = rays_to(target, noise_deg=0.05, rng=rng)
    cams4 = dict(CAMS, **{"Камера 4": GeoPoint(50.4480, 30.5300, 170.0)})
    a4, e4, _ = bearing_elevation(FRAME, cams4["Камера 4"], target)
    rays.append(make_ray(FRAME, cams4["Камера 4"], "Камера 4", 0, a4 + 5.0, e4))     # хибний на 5°
    res = solve(rays, FRAME, P)
    assert len(res) == 1
    t = res[0]
    assert sorted(t.cameras) == ["Камера 1", "Камера 2", "Камера 3"]
    assert geo_error_m(t.geo, target) <= P.tolerance(t.distance_m) / 2
    assert len(t.pairs) == 3 and t.error_m < 1.0


def test_three_cameras_better_than_two():
    rng = random.Random(11)
    err2, err3 = [], []
    for _ in range(100):
        target = GeoPoint(50.4525 + rng.uniform(-0.004, 0.004), 30.5235 + rng.uniform(-0.004, 0.004), 400.0)
        rays = rays_to(target, noise_deg=0.1, rng=rng)
        err3.append(geo_error_m(solve(rays, FRAME, P)[0].geo, target))
        err2.append(geo_error_m(solve(rays[:2], FRAME, P)[0].geo, target))
    assert np.mean(err3) < np.mean(err2)


def test_confirmation_needs_consecutive_frames():
    target = GeoPoint(50.4530, 30.5240, 330.0)
    two = {k: CAMS[k] for k in ("Камера 1", "Камера 2")}
    conf = Confirmation(3)
    flags = [conf.update(solve(rays_to(target, two, obj=7), FRAME, P))[0].confirmed for _ in range(3)]
    assert flags == [False, False, True]
    conf.update([])                                          # кадр без пари — лічба з початку
    assert not conf.update(solve(rays_to(target, two, obj=7), FRAME, P))[0].confirmed


def test_from_settings_window_defaults_to_frame_time():
    from polar_core.detection import DetectionSettings
    s = DetectionSettings()
    m = MatchParams.from_settings(s, fps=25)
    assert m.sync_window_s == pytest.approx(0.04) and m.k == 2.0 and m.accuracy_deg == 0.5
    s.sync_window_ms = 100
    assert MatchParams.from_settings(s, fps=25).sync_window_s == pytest.approx(0.1)
    assert math.isclose(m.min_angle_deg, 5.0) and m.confirm_frames == 3
