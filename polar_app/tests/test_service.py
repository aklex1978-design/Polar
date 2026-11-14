import pytest

from polar_app.control.service import ControlService
from polar_app.control.sim import SimControlBackend
from polar_core import control as ctl

from conftest import Clock


@pytest.fixture
def env(conn):
    clock = Clock()
    backend = SimControlBackend(clock=clock, autostart_thread=False)
    svc = ControlService(conn, backend, clock=clock)
    svc.start()

    def run(seconds, dt=0.05):
        for _ in range(int(round(seconds / dt))):
            clock.t += dt
            backend.tick()
            sts = {n: svc.status(n) for n in svc.camera_names()}   # як таймер інтерфейсу
            backend._pool.submit(lambda: None).result()    # дочекатися команд у черзі
        return sts
    return svc, backend, clock, run


def test_cameras_online_and_config_pushed(env):
    svc, backend, clock, run = env
    sts = run(0.2)
    assert all(s.link_ok for s in sts.values())
    run(0.1)
    assert svc.last_configure["Камера 1"] == (True, "налаштування застосовано")


def test_go_to_uses_north_offset(env):
    svc, backend, clock, run = env
    run(0.1)
    assert svc.go_to("Камера 2", 40.0, 30.0, 180.0) == pytest.approx((40.0, 30.0))   # зміщення 30°
    st = run(1.0)["Камера 2"]
    assert st.azimuth_deg == pytest.approx(40.0) and st.elevation_deg == pytest.approx(30.0)
    assert backend.camera("Камера 2").ctrl.state.azimuth_deg == pytest.approx(10.0)  # кут камери


def test_limits_are_applied(env):
    svc, backend, clock, run = env
    assert svc.limit_note("Камера 1", 10.0, 120.0) == "Досягнуто межі: кут місця — верхня межа 90°"
    assert svc.limit_note("Камера 1", 10.0, 45.0) == ""
    assert svc.go_to("Камера 1", 10.0, 120.0) == pytest.approx((10.0, 90.0))


def test_jog_reports_limit(env):
    svc, backend, clock, run = env
    run(0.1)
    assert svc.jog("Камера 1", 0.0, -5.0, 60.0) == "Досягнуто межі: кут місця — нижня межа 0°"
    assert svc.jog("Камера 1", 0.0, 5.0, 60.0) == ""
    svc.go_to("Камера 1", 0.0, 88.0, 180.0)
    run(1.0)
    assert "верхня межа 90°" in svc.jog("Камера 1", 0.0, 5.0, 60.0)
    assert svc.jog("Камера 1", 10.0, 0.0, 60.0) == ""               # азимут без меж (ковзний контакт)


def test_jog_and_stop(env):
    svc, backend, clock, run = env
    run(0.1)
    svc.jog("Камера 1", -10.0, 5.0, 60.0)
    st = run(1.0)["Камера 1"]
    assert st.azimuth_deg == pytest.approx(350.0) and st.elevation_deg == pytest.approx(5.0)
    svc.go_to("Камера 1", 170.0, 0.0, 60.0)
    run(0.5)
    svc.stop("Камера 1").result()
    st = run(1.0)["Камера 1"]
    assert st.error_flags & ctl.ERROR_STOPPED and not st.moving
    assert 360.0 - 20 > st.azimuth_deg > 300.0 or st.azimuth_deg < 40.0


def test_learning_blocks_manual_but_not_stop(env):
    svc, backend, clock, run = env
    svc.set_learning("Камера 1", True)
    with pytest.raises(ctl.ManualControlLocked):
        svc.go_to("Камера 1", 10, 10)
    with pytest.raises(ctl.ManualControlLocked):
        svc.jog("Камера 1", 1, 0)
    svc.go_to("Камера 1", 10, 10, source="learning")      # Навчання саме керує камерою
    svc.stop("Камера 1").result()
    assert not svc.is_learning("Камера 1")


def test_link_loss_and_reconfigure_on_return(env):
    svc, backend, clock, run = env
    run(0.2)
    svc.go_to("Камера 3", 170.0, 0.0, 30.0)
    run(0.5)
    backend.set_online("Камера 3", False)
    st = run(3.0)["Камера 3"]
    assert not st.link_ok and st.link_text == "немає звʼязку"
    assert not backend.camera("Камера 3").ctrl.state.moving   # Pi зупинив сервоприводи
    with pytest.raises(TimeoutError):
        svc.scan("Камера 3").result()
    svc.last_configure.pop("Камера 3", None)
    backend.set_online("Камера 3", True)
    run(0.3)
    assert svc.status("Камера 3").link_ok
    assert svc.last_configure["Камера 3"][0]


def test_save_config_persists_and_applies(env, conn):
    svc, backend, clock, run = env
    run(0.2)
    cfg = svc.config("Камера 1")
    cfg.tilt.min_deg, cfg.tilt.max_deg = 10.0, 45.0
    ok, msg = svc.save_config(cfg).result()
    assert ok
    assert ctl.load_control_configs(conn)[0].tilt.max_deg == 45.0
    svc.backend.command("Камера 1", 0.0, 80.0, 180.0)        # навіть повз сервер — Pi обмежує сам
    assert run(1.0)["Камера 1"].elevation_deg == pytest.approx(45.0)
    cfg.tilt.servo_id = cfg.pan.servo_id
    with pytest.raises(ValueError):
        svc.save_config(cfg)


def test_set_zero_saved_to_db(env, conn):
    svc, backend, clock, run = env
    run(0.1)
    svc.go_to("Камера 1", 25.0, 15.0)
    run(1.0)
    assert svc.set_zero("Камера 1", "pan").result() == pytest.approx(25.0)
    assert ctl.load_control_configs(conn)[0].pan.zero_offset_deg == pytest.approx(25.0)
    assert run(0.1)["Камера 1"].azimuth_deg == pytest.approx(0.0, abs=1e-6)


def test_scan_and_set_id(env):
    svc, backend, clock, run = env
    run(0.1)
    assert svc.scan("Камера 1", 1, 10).result() == [1, 2]
    assert svc.set_servo_id("Камера 1", 2, 5).result()[0]
    assert svc.scan("Камера 1", 1, 10).result() == [1, 5]
    st = run(0.2)["Камера 1"]
    assert st.error_flags & ctl.ERROR_SERVO_OFFLINE           # налаштування ще чекають ID 2
    cfg = svc.config("Камера 1")
    cfg.tilt.servo_id = 5
    assert svc.save_config(cfg).result()[0]
    assert not run(0.2)["Камера 1"].error_flags & ctl.ERROR_SERVO_OFFLINE


def test_free_limits_for_zeroing(env):
    """«Зняти межі»: кут місця нижче 0° і вище 90° дозволено; у базі межі ті самі; знову — обмежено."""
    svc, backend, clock, run = env
    run(0.2)
    import copy
    cfg = copy.deepcopy(svc.config("Камера 1"))
    cfg.tilt.zero_offset_deg = 180.0                  # горизонт — посередині ходу вала (raw 180°)
    svc.save_config(cfg)
    run(0.2)
    assert svc.go_to("Камера 1", 10.0, -10.0) == pytest.approx((10.0, 0.0))        # межі 0…90°
    f = svc.free_limits("Камера 1", True)
    run(0.2)
    assert f.result(1)[0] and svc.limits_free("Камера 1")
    assert svc.limit_note("Камера 1", 10.0, -10.0) == ""
    assert svc.go_to("Камера 1", 10.0, -10.0, 180.0) == pytest.approx((10.0, -10.0))
    st = run(1.0)["Камера 1"]
    assert st.elevation_deg == pytest.approx(-10.0, abs=0.2)                    # Pi теж пустив нижче нуля
    z = svc.set_zero("Камера 1", "tilt")
    run(0.2)
    assert z.result(1) == pytest.approx(170.0, abs=0.2)                         # тут тепер нуль (raw 170°)
    assert svc.config("Камера 1").tilt.min_deg == 0.0                           # межі в базі не змінено
    svc.free_limits("Камера 1", False)
    run(0.2)
    assert svc.go_to("Камера 1", 10.0, 120.0) == pytest.approx((10.0, 90.0))


def test_free_limits_stop_at_shaft_end(env):
    """Нуль на краю ходу вала (raw 0°): нижче нуля вал у режимі положення не піде — напис це показує."""
    svc, backend, clock, run = env
    run(0.2)
    svc.free_limits("Камера 1", True)
    run(0.2)
    note = svc.limit_note("Камера 1", 10.0, -10.0)
    assert note.startswith("Кінець ходу вала") and "нижня межа 0°" in note


def test_recenter_zero(env):
    """«Нуль + середина ходу»: вал вертикального сервопривода — 180°, нуль у базі 180°; нижче горизонту — можна."""
    svc, backend, clock, run = env
    run(0.2)
    z = svc.set_zero("Камера 1", "tilt", recenter=True)
    run(0.2)
    assert z.result(1) == pytest.approx(180.0)
    assert svc.config("Камера 1").tilt.zero_offset_deg == pytest.approx(180.0)
    with svc._db_lock:
        row = svc.conn.execute("SELECT zero_offset_deg FROM servo_settings s JOIN cameras c ON c.id = s.camera_id "
                               "WHERE c.name = 'Камера 1' AND s.axis = 'tilt'").fetchone()
    assert row[0] == pytest.approx(180.0)
    svc.free_limits("Камера 1", True)
    run(0.2)
    assert svc.limit_note("Камера 1", 10.0, -10.0) == ""                      # хід вала тепер ±180° від нуля
