

def test_limit_plan_stays_within_azimuth_limits():
    """Межі азимуту 40…200°: вузли поза межами відкинуто, на межах — вузли, змійка збережена."""
    from polar_core.learning import learning_plan, limit_plan
    plan = learning_plan(60.0, 34.0, 0.0, 40.0)                    # крок 30°: 0, 30, … 330
    lim = limit_plan(plan, 40.0, 200.0)
    assert all(40.0 <= az <= 200.0 for az, _el in lim)
    rows = {}
    for az, el in lim:
        rows.setdefault(el, []).append(az)
    first, second = list(rows.values())[:2]
    assert first == [40.0, 60.0, 90.0, 120.0, 150.0, 180.0, 200.0]
    assert second == list(reversed(first))                         # змійка
    assert limit_plan(plan, 0.0, 330.0)[:3] == [(0.0, 0.0), (30.0, 0.0), (60.0, 0.0)]   # без зайвих країв
