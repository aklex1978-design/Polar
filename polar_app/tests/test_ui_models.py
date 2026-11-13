"""Екран «Налаштування / Моделі камер» і вибір моделі у формі камери (QT_QPA_PLATFORM=offscreen)."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402

from polar_app.__main__ import build_context  # noqa: E402
from polar_app.ui.main_window import MainWindow  # noqa: E402

import fakes  # noqa: E402
from demo_data import seed_demo  # noqa: E402
from polar_core import db  # noqa: E402


def no_ros():
    raise ImportError("No module named 'rclpy'")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(app, tmp_path):
    path = str(tmp_path / "polar.sqlite")
    seed_demo(db.connect(path))
    ctx = fakes.bind(build_context(path, video_options=fakes.video_options(), ros_factory=no_ros))
    w = MainWindow(ctx)
    w.show()
    yield w
    w.close()


def open_models(win):
    act = next(a for m in win.menuBar().actions() if m.text() == "Налаштування"
               for a in m.menu().actions() if a.text() == "Моделі камер")
    act.trigger()
    assert win.stack.currentWidget() is win.models
    return win.models


def test_models_list_add_edit_delete(win):
    s = open_models(win)
    assert s.table.rowCount() == 1 and s.table.item(0, 0).text() == "демо" and s.table.item(0, 3).text() == "3"
    assert not s.delete() and "використовують" in s.message.text()      # модель у камерах — не видаляється
    s.new_btn.click()
    assert not s.save_btn.isEnabled() and s.errors["name"].isVisible()
    s.widgets["name"].setText("ДЕМО")
    assert "вже є" in s.errors["name"].text()
    s.widgets["name"].setText("Тепловізор 256")
    s.widgets["fov_x_deg"].setValue(24)
    s.widgets["fov_y_deg"].setValue(18)
    s.sensor_x.setText("256")
    assert s.errors["sensor_x"].isVisible()                               # обидва розміри або жодного
    s.sensor_y.setText("19x")
    assert s.errors["sensor_x"].text() == "цілі числа"
    s.sensor_y.setText("192")
    assert s.save_btn.isEnabled()
    assert s.save() and s.table.rowCount() == 2
    row = next(r for r in range(2) if s.table.item(r, 0).text() == "Тепловізор 256")
    assert s.table.item(row, 2).text() == "256×192" and s.table.item(row, 3).text() == "0"
    assert s.delete() and s.table.rowCount() == 1


def test_edit_model_updates_cameras_and_form_is_read_only(win):
    s = open_models(win)
    s.widgets["fov_x_deg"].setValue(70)
    assert s.save()
    assert "застарілими" in s.message.text()
    assert all(r.fov_x_deg == 70 and r.panorama_stale for r in win.ctx.records())
    rec = win.ctx.records()[0]
    win.edit_camera(rec.id)
    f = win.form
    assert f.widgets["model_id"].currentText().startswith("демо")
    assert isinstance(f.widgets["fov_x_deg"], QtWidgets.QLabel) and f.widgets["fov_x_deg"].text() == "70"
    # «Моделі…» з форми → нова модель → у формі зʼявилась у списку, вибір камери збережено
    f.models_btn.click()
    assert win.stack.currentWidget() is win.models
    s.new_btn.click()
    s.widgets["name"].setText("Денна 4 мм")
    s.widgets["fov_x_deg"].setValue(90)
    s.widgets["fov_y_deg"].setValue(50)
    assert s.save()
    w = f.widgets["model_id"]
    assert w.currentText().startswith("демо") and w.findText("Денна 4 мм", Qt.MatchFlag.MatchStartsWith) > 0
