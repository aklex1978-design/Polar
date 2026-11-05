"""Перевірка екрана «Камери / Управління» з імітатором (без дисплея: QT_QPA_PLATFORM=offscreen)."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from polar_app.control.service import ControlService  # noqa: E402
from polar_app.control.sim import SimControlBackend  # noqa: E402
from polar_app.ui.control_screen import ControlScreen  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def wait(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if until and until():
            return True
        time.sleep(0.01)
    return until() if until else True


@pytest.fixture
def screen(app, conn):
    backend = SimControlBackend()
    svc = ControlService(conn, backend)
    svc.start()
    s = ControlScreen(svc)
    s.resize(1100, 820)
    s.show()
    yield s, svc, backend
    s.timer.stop()
    svc.shutdown()
    s.close()


def test_list_and_link_status(screen):
    s, svc, backend = screen
    assert s.camera_list.count() == 3
    assert wait(2, lambda: "на звʼязку" in s.camera_list.item(0).text())
    backend.set_online("Камера 2", False)
    assert wait(4, lambda: "немає звʼязку" in s.camera_list.item(1).text())


def test_go_to_and_stop(screen):
    s, svc, backend = screen
    s.select_camera("Камера 2")                    # зміщення від півночі 30°
    wait(0.3)
    s.tgt_az.setValue(100.0)
    s.tgt_el.setValue(20.0)
    s.speed.setValue(360.0)
    s.go_btn.click()
    assert wait(3, lambda: s.cur_az.text() == "Азимут: 100.0°" and s.cur_el.text() == "Кут місця: 20.0°"), \
        (s.cur_az.text(), s.cur_el.text())
    s.speed.setValue(10.0)
    s.btn_right.click()
    s.step.setValue(30.0)
    s.btn_right.click()
    wait(0.3)
    s.stop_btn.click()
    assert wait(2, lambda: "зупинено" in s.error_label.text())
    assert wait(2, lambda: s.cur_move.text() == "Рух: стоїть")


def test_learning_disables_manual(screen):
    s, svc, backend = screen
    wait(0.3)
    svc.set_learning("Камера 1", True)
    assert wait(1, lambda: not s.go_btn.isEnabled())
    assert s.stop_btn.isEnabled()
    svc.set_learning("Камера 1", False)
    assert wait(1, lambda: s.go_btn.isEnabled())


def test_save_settings_and_validation(screen):
    s, svc, backend = screen
    wait(0.3)
    s.cols["tilt"].max_deg.setValue(60.0)
    s.save_btn.click()
    assert wait(2, lambda: "застосовано" in s.message.text()), s.message.text()
    assert svc.config("Камера 1").tilt.max_deg == 60.0
    s.cols["tilt"].servo_id.setValue(1)             # той самий ID, що й у горизонтального
    s.save_btn.click()
    assert "Не збережено" in s.message.text()
    s.revert_btn.click()
    assert s.cols["tilt"].servo_id.value() == 2


def test_scan_zero_and_change_id(screen):
    s, svc, backend = screen
    wait(0.3)
    s.scan_btn.click()
    assert wait(2, lambda: "ID: 1, 2" in s.message.text()), s.message.text()
    s.change_servo_id(2, 7)
    assert wait(2, lambda: "ID змінено" in s.message.text()), s.message.text()
    s._on_zero(("pan",))
    assert wait(2, lambda: "збережено" in s.message.text()), s.message.text()


def test_telemetry_shown(screen):
    s, svc, backend = screen
    assert wait(2, lambda: "°C" in s.cols["pan"].tel_pos.text())
    assert "В ·" in s.cols["tilt"].tel_power.text()
    backend.camera("Камера 1").bus.servos[2].online = False
    assert wait(2, lambda: s.cols["tilt"].tel_pos.text() == "немає відповіді")
    assert "сервопривід не відповідає" in s.error_label.text()
