"""Екрани «Камери» і «Камери / Редагування» з імітатором (QT_QPA_PLATFORM=offscreen)."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from polar_app.__main__ import build_context  # noqa: E402
from polar_app.ui.main_window import MainWindow  # noqa: E402
from polar_core import cameras as cm  # noqa: E402


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
def win(app, tmp_path):
    ctx = build_context(str(tmp_path / "polar.sqlite"), sim=True, demo=True)
    w = MainWindow(ctx, "тест")
    w.show()
    yield w
    w.close()


def test_list_shows_cameras_and_buttons(win):
    t = win.cameras.table
    assert t.rowCount() == 3
    assert [t.item(r, 0).text() for r in range(3)] == ["Камера 1", "Камера 2", "Камера 3"]
    assert [t.cellWidget(0, c).text() for c in (2, 3, 4)] == ["Калібрування", "Редагувати", "Видалити"]
    assert t.columnCount() == 5 and t.horizontalHeaderItem(0).text() == "Назва"


def test_add_camera_validation_and_save(win):
    win.cameras.add_btn.click()
    f = win.form
    assert win.stack.currentWidget() is f
    assert not f.save_btn.isEnabled()                        # порожні обовʼязкові поля
    assert f.errors["name"].isVisible() and f.errors["latitude"].isVisible()
    f.widgets["name"].setText("Камера 4")
    f.widgets["latitude"].setText("50,46")                   # кома теж працює
    f.widgets["longitude"].setText("30.6")
    f.widgets["altitude_m"].setText("abc")
    assert f.errors["altitude_m"].text() == "введіть число"
    f.widgets["altitude_m"].setText("175")
    f.widgets["address"].setText("rtsp://admin:1@cam/x")
    assert "окремих полях" in f.errors["address"].text()
    f.widgets["address"].setText("rtsp://10.0.0.14/stream1")
    f.widgets["login"].setText("admin")
    f.widgets["password"].setText("секрет")
    f.widgets["stream_res_x"].setText("640")
    f.widgets["stream_res_y"].setText("360")
    assert f.save_btn.isEnabled(), {k: v.text() for k, v in f.errors.items() if v.isVisible()}
    assert f.save()
    assert "Збережено" in f.message.text()
    recs = win.ctx.records()
    new = [r for r in recs if r.name == "Камера 4"][0]
    assert new.latitude == pytest.approx(50.46) and new.has_password
    assert cm.stream_password(win.ctx.conn, win.ctx.box, new.id) == "секрет"
    assert "Камера 4" in win.control._items                  # з'явилась і в «Управлінні»
    assert wait(6, lambda: win.ctx.video.stats("Камера 4") and win.ctx.video.stats("Камера 4").frames > 3)


def test_edit_marks_panorama_stale_and_rename(win):
    rec = win.ctx.records()[0]
    win.edit_camera(rec.id)
    f = win.form
    assert f.widgets["name"].text() == "Камера 1"
    f.widgets["fov_x_deg"].setValue(65.0)
    f.widgets["name"].setText("Північна")
    assert f.save()
    assert "застарілою" in f.message.text()
    assert win.ctx.camera(rec.id).panorama_stale
    assert "Північна" in win.control._items and "Камера 1" not in win.control._items
    f.cancel_btn.click()
    assert win.stack.currentWidget() is win.cameras
    assert win.cameras.table.item(0, 0).text() == "Північна"


def test_duplicate_name_blocked(win):
    rec = win.ctx.records()[1]
    win.edit_camera(rec.id)
    win.form.widgets["name"].setText("камера 1")
    assert not win.form.save_btn.isEnabled()
    assert "вже є" in win.form.errors["name"].text()


def test_delete_with_confirmation(win):
    asked = []
    win.cameras.confirm = lambda text: asked.append(text) or False
    rec = win.ctx.records()[2]
    win.cameras.table.cellWidget(2, 4).click()
    assert asked and win.cameras.table.rowCount() == 3       # відмовились — нічого не видалено
    win.cameras.confirm = lambda text: True
    win.cameras.table.cellWidget(2, 4).click()
    assert win.cameras.table.rowCount() == 2
    assert rec.name not in win.control._items
    assert win.ctx.video.stats(rec.name) is None


def test_live_frame_with_angles_and_check(win):
    rec = win.ctx.records()[0]
    win.edit_camera(rec.id)
    f = win.form
    assert wait(6, lambda: "азимут" in f.frame_info.text()), f.frame_info.text()
    assert wait(3, lambda: "кадрів/с" in f.stream_info.text())
    assert "без кутів: 0" in f.stream_info.text()
    f.check_btn.click()
    assert wait(15, lambda: "Зʼєднання працює" in f.message.text()), f.message.text()


def test_measure_latency_button(win):
    rec = win.ctx.records()[1]
    win.edit_camera(rec.id)
    f = win.form
    assert wait(5, lambda: win.ctx.control.status(rec.name).link_ok)
    wait(0.5)
    f.latency_btn.click()
    assert wait(30, lambda: "Затримка відео" in f.message.text() or "не виміряно" in f.message.text())
    assert "Затримка відео" in f.message.text(), f.message.text()
    assert 100 <= f.widgets["video_latency_ms"].value() <= 220


def test_usb_camera_list_fills_form(win):
    win.cameras.add_btn.click()
    f = win.form
    assert not f.usb_btn.isEnabled()                         # для RTSP кнопка неактивна
    f.widgets["protocol"].setCurrentIndex(f.widgets["protocol"].findData("USB"))
    assert f.usb_btn.isEnabled()
    f.usb_btn.click()
    assert wait(5, lambda: getattr(f, "usb_dialog", None) is not None and f.usb_dialog.isVisible())
    d = f.usb_dialog
    assert d.table.rowCount() == 2 and d.table.item(0, 0).text() == "/dev/video0"
    assert d.mode.count() == 3 and d.selected_mode().width == 1920      # найкращий режим за замовчуванням
    d.mode.setCurrentIndex(1)                                            # 1280×720, 60 кадрів/с
    d.accept()
    wait(0.2)
    assert f.widgets["address"].text() == "/dev/v4l/by-id/usb-sim_HD_USB_Camera-video-index0"
    assert (f.widgets["stream_res_x"].text(), f.widgets["stream_res_y"].text()) == ("1280", "720")
    assert f.widgets["fps"].value() == 60.0 and f.widgets["codec"].currentData() == "MJPEG"
    assert f.widgets["model"].text() == "Імітатор: HD USB Camera"
    assert "address" not in f.validate()
    # вже додана камера позначається в списку
    f.widgets["name"].setText("USB 1")
    for k, v in (("latitude", "50.1"), ("longitude", "30.1"), ("altitude_m", "100")):
        f.widgets[k].setText(v)
    assert f.save()
    win.cameras.add_btn.click()
    f.widgets["protocol"].setCurrentIndex(f.widgets["protocol"].findData("USB"))
    f.usb_btn.click()
    assert wait(5, lambda: f.usb_dialog.isVisible() and f.usb_dialog.table.rowCount() == 2)
    assert f.usb_dialog.table.item(0, 2).text() == "USB 1"
    assert f.usb_dialog.table.currentRow() == 1                           # вибрано вільну камеру
    f.usb_dialog.reject()


def test_new_camera_form_has_no_stale_frame(win):
    win.edit_camera(win.ctx.records()[0].id)
    assert wait(6, lambda: "азимут" in win.form.frame_info.text())
    win.edit_camera(None)
    wait(0.5)
    assert win.form.frame_info.text() == "" and win.form.preview.pixmap().isNull()
