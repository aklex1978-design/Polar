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
    assert svc.go_to("Камера 1", 10.0, 120.0) == pytest.approx((10.0, 90.0))


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
