"""Огляд зони і стеження за підозрілою ціллю (без відео і сервоприводів)."""
from types import SimpleNamespace as Obj

import pytest

from polar_core.patrol import ALERT, PATROL, Patroller, near_edge, patrol_plan


def obj(az, el, u, v, area=100.0):
    return Obj(az=az, el=el, u=u, v=v, area_px=area)


def test_plan_full_circle_and_limits():
    plan = patrol_plan(90, 60, 20, 20, 0.8, north_offset_deg=10)
    assert plan == [pytest.approx((a, 20.0)) for a in (10, 82, 154, 226, 298)]
    rows = {el for _az, el in patrol_plan(60, 34, 0, 30, 1.0)}
    assert rows == {0.0, 30.0}                     # крок 34°: ряд 0° (верх кадру 17°) і ряд 30°
    lim = patrol_plan(60, 34, 5, 5, 1.0, pan_limits=(60.0, 200.0))
    azs = [a for a, _ in lim]
    assert azs[0] == pytest.approx(60.0) and azs[-1] == pytest.approx(200.0)
    assert all(60.0 - 1e-6 <= a <= 200.0 + 1e-6 for a in azs)


def test_near_edge():
    assert near_edge(10, 240, 640, 480, 0.2) and near_edge(600, 240, 640, 480, 0.2)
    assert not near_edge(320, 240, 640, 480, 0.2) and not near_edge(10, 240, 640, 480, 0.0)


def step(p, t, pose, still=True, compared=True, objects=()):
    return p.update(t, pose, still, compared, objects, (640, 480))


def test_patrol_dwells_then_moves_and_bounces_in_limits():
    p = Patroller([(0, 10), (50, 10), (100, 10)], dwell_s=2.0, hold_s=3.0, edge_frac=0.2, cyclic=False)
    assert step(p, 0.0, (300, 0), still=False) == (0, 10)             # перше положення
    assert step(p, 0.5, (150, 5), still=False) is None                 # ще йде
    assert step(p, 1.0, (0.5, 10)) is None                              # дійшла — огляд
    assert step(p, 2.5, (0.5, 10)) is None
    assert step(p, 3.1, (0.5, 10)) == (50, 10)                          # час огляду минув — далі
    t = 3.1
    targets = []
    for _ in range(4):
        cur = p.target
        t += 1.0
        step(p, t, cur)
        t += 2.1
        targets.append(step(p, t, cur))
    assert targets == [(100, 10), (50, 10), (0, 10), (50, 10)]         # у межах — туди й назад


def test_motion_stops_patrol_follows_edge_and_resumes():
    p = Patroller([(0, 10), (50, 10)], dwell_s=2.0, hold_s=3.0, edge_frac=0.2)
    step(p, 0.0, (0, 10))
    step(p, 0.1, (0, 10))
    assert step(p, 1.0, (0, 10), objects=[obj(5, 12, 400, 200)]) is None   # рух у центрі — стоїть, підозра
    assert p.state == ALERT and p.suspect.az == 5 and "ПІДОЗРА" in p.text()
    assert step(p, 5.0, (0, 10), objects=[obj(8, 12, 420, 200)]) is None   # довше за час огляду — не йде далі
    assert step(p, 6.0, (0, 10), objects=[obj(25, 12, 610, 200)]) == (25, 12)   # біля краю — за ціллю
    assert p.suspect.follows == 1
    assert step(p, 6.5, (12, 11), still=False) is None
    assert step(p, 7.0, (25, 12)) is None                               # дійшла; ціль зникла
    assert step(p, 9.0, (25, 12)) is None
    assert step(p, 10.1, (25, 12)) == (0, 10)                           # час утримання минув — огляд далі
    assert p.state == PATROL and p.suspect is None


def test_objects_while_moving_ignored():
    p = Patroller([(0, 10), (90, 10)], dwell_s=1.0, hold_s=3.0, edge_frac=0.2)
    step(p, 0.0, (0, 10))
    assert step(p, 0.5, (40, 10), still=False, objects=[obj(40, 10, 320, 240)]) is None
    assert p.state == PATROL


def test_cue_search_point_and_release():
    from polar_core.patrol import CUE, Cue
    p = Patroller([(0, 10), (90, 10)], dwell_s=1.0, hold_s=2.0, edge_frac=0.2)
    step(p, 0.0, (0, 10))
    step(p, 0.1, (0, 10))
    p.cue(Cue("search", [(100, 5), (110, 6), (120, 7)], "променю «Камера 1»", ("search", "Камера 1")))
    assert step(p, 0.2, (0, 10)) == (100, 5) and p.state == CUE
    assert "НАВЕДЕННЯ: пошук" in p.text()
    p.cue(Cue("search", [(100, 5), (110, 6), (120, 7)], "x", ("search", "Камера 1")))   # той самий — без змін
    assert step(p, 0.3, (50, 8), still=False) is None
    assert step(p, 1.0, (100, 5)) is None
    assert step(p, 2.1, (100, 5)) == (110, 6)                          # вузли по черзі
    assert step(p, 2.5, (110, 6)) is None
    assert step(p, 3.6, (110, 6)) == (120, 7)
    assert step(p, 3.7, (120, 7)) is None
    assert step(p, 4.8, (120, 7)) == (110, 6)                          # туди й назад
    p.cue(Cue("point", [(200, 15)], "ціль №1", ("point", 1), tol_deg=3.0))   # ціль з координатами
    assert step(p, 5.0, (110, 6)) == (200, 15) and "ціль №1" in p.text()
    p.cue(Cue("point", [(201, 15)], "ціль №1", ("point", 1), tol_deg=3.0))
    assert step(p, 5.1, (150, 10), still=False) is None                # зсув менший за допуск
    p.cue(Cue("point", [(206, 15)], "ціль №1", ("point", 1), tol_deg=3.0))
    assert step(p, 5.2, (150, 10), still=False) == (206, 15)
    assert step(p, 6.0, (206, 15), objects=[obj(206, 15, 320, 240)]) is None   # побачила — своя підозра
    assert p.state == ALERT
    p.cue(Cue("point", [(250, 15)], "ціль №1", ("point", 1)))
    assert step(p, 6.1, (206, 15), objects=[obj(206, 15, 320, 240)]) is None and p.state == ALERT
    step(p, 9.0, (206, 15))
    p.cue(None)
    step(p, 9.1, (206, 15))
    assert p.state == PATROL


def test_cue_release_returns_to_patrol():
    from polar_core.patrol import Cue
    p = Patroller([(0, 10), (90, 10)], dwell_s=1.0, hold_s=2.0, edge_frac=0.2)
    step(p, 0.0, (0, 10))
    p.cue(Cue("search", [(100, 5)], "x", ("search", "A")))
    assert step(p, 0.1, (0, 10)) == (100, 5)
    p.cue(None)
    assert step(p, 0.2, (50, 5), still=False) == (0, 10) and p.state == PATROL
