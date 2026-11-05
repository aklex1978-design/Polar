import pytest

from pantilt_driver.backend import SimBackend
from pantilt_driver.controller import (
    ERROR_LINK_TIMEOUT, ERROR_NONE, ERROR_OVERHEAT, ERROR_SERVO_OFFLINE, ERROR_STOPPED,
    REBASE_THRESHOLD_DEG, PanTiltController,
)
from pantilt_driver.servo import ServoConfig


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(link_timeout_s=2.0, rebase_ok=True):
    clock = Clock()
    backend = SimBackend([1, 2], clock=clock)
    if not rebase_ok:
        def no_rebase(servo_id, turns):
            raise NotImplementedError
        backend.rebase = no_rebase
    pan = ServoConfig(servo_id=1, continuous=True, max_speed_deg_s=180)
    tilt = ServoConfig(servo_id=2, min_deg=0, max_deg=90, max_speed_deg_s=180)
    ctrl = PanTiltController(backend, pan, tilt, link_timeout_s=link_timeout_s, clock=clock)
    return ctrl, backend, clock


def run(ctrl, clock, seconds, dt=0.05, heartbeat=True):
    for _ in range(int(seconds / dt)):
        clock.t += dt
        if heartbeat:
            ctrl.heartbeat()
        st = ctrl.update()
    return st


def test_move_to_target():
    ctrl, _, clock = make()
    ctrl.command(90.0, 45.0, 90.0)
    st = run(ctrl, clock, 0.5)
    assert st.moving
    st = run(ctrl, clock, 1.0)
    assert not st.moving
    assert st.azimuth_deg == pytest.approx(90.0)
    assert st.elevation_deg == pytest.approx(45.0)
    assert st.error_flags == ERROR_NONE


def test_tilt_is_clamped():
    ctrl, _, clock = make()
    ctrl.command(0.0, 120.0)
    st = run(ctrl, clock, 2.0)
    assert st.elevation_deg == pytest.approx(90.0)


def test_azimuth_shortest_path_across_zero():
    ctrl, backend, clock = make()
    ctrl.command(350.0, 0.0)
    run(ctrl, clock, 1.0)
    assert backend.servos[1].raw_deg == pytest.approx(-10.0)  # 10° назад, а не 350° вперед
    ctrl.command(20.0, 0.0)
    st = run(ctrl, clock, 1.0)
    assert backend.servos[1].raw_deg == pytest.approx(20.0)
    assert st.azimuth_deg == pytest.approx(20.0)


def test_jog():
    ctrl, _, clock = make()
    ctrl.command(10.0, 10.0)
    run(ctrl, clock, 1.0)
    ctrl.jog(-20.0, 5.0)
    st = run(ctrl, clock, 1.0)
    assert st.azimuth_deg == pytest.approx(350.0)
    assert st.elevation_deg == pytest.approx(15.0)


def test_stop_and_resume():
    ctrl, _, clock = make()
    ctrl.command(170.0, 0.0, 90.0)
    run(ctrl, clock, 0.5)
    ctrl.stop()
    st = run(ctrl, clock, 2.0)
    assert st.error_flags & ERROR_STOPPED
    assert 30.0 < st.azimuth_deg < 60.0
    ctrl.command(170.0, 0.0)
    st = run(ctrl, clock, 2.0)
    assert not st.error_flags & ERROR_STOPPED
    assert st.azimuth_deg == pytest.approx(170.0)


def test_link_timeout_halts():
    ctrl, _, clock = make(link_timeout_s=1.0)
    ctrl.command(170.0, 0.0, 30.0)
    st = run(ctrl, clock, 2.0, heartbeat=False)
    assert st.error_flags & ERROR_LINK_TIMEOUT
    assert not st.moving
    assert st.azimuth_deg < 40.0
    ctrl.heartbeat()
    assert not ctrl.update().error_flags & ERROR_LINK_TIMEOUT


def test_servo_offline():
    ctrl, backend, clock = make()
    backend.servos[2].online = False
    st = run(ctrl, clock, 0.1)
    assert st.error_flags & ERROR_SERVO_OFFLINE
    assert not st.tilt.online and st.pan.online


def test_overheat_halts():
    ctrl, backend, clock = make()
    ctrl.command(170.0, 0.0, 30.0)
    run(ctrl, clock, 0.5)
    backend.servos[1].temperature_c = 80.0
    st = run(ctrl, clock, 1.0)
    assert st.error_flags & ERROR_OVERHEAT
    assert not st.moving


def test_rebase_after_many_turns():
    ctrl, backend, clock = make()
    for _ in range(8):  # 8 обертів в один бік по 120°×3
        for az in (120.0, 240.0, 0.0):
            ctrl.command(az, 0.0)
            run(ctrl, clock, 1.0)
    raw = backend.servos[1].raw_deg
    assert abs(raw) < REBASE_THRESHOLD_DEG
    assert ctrl.state.azimuth_deg == pytest.approx(0.0, abs=1e-6)


def test_no_rebase_support_is_tolerated():
    ctrl, backend, clock = make(rebase_ok=False)
    backend.servos[1].raw_deg = backend.servos[1].goal_deg = 6 * 360.0 + 30.0  # 6,08 оберту — вище порогу
    assert backend.servos[1].raw_deg > REBASE_THRESHOLD_DEG
    st = run(ctrl, clock, 0.1)
    assert st.azimuth_deg == pytest.approx(30.0)
    assert backend.servos[1].raw_deg == pytest.approx(6 * 360.0 + 30.0)  # лічильник не скинуто


def test_same_ids_rejected():
    backend = SimBackend([1])
    with pytest.raises(ValueError):
        PanTiltController(backend, ServoConfig(servo_id=1), ServoConfig(servo_id=1))
