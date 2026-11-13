"""Екрани «Камери» і «Камери / Редагування» з імітатором (QT_QPA_PLATFORM=offscreen)."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QCoreApplication, Qt  # noqa: E402

from polar_app.__main__ import build_context  # noqa: E402
from polar_app.ui.main_window import MainWindow  # noqa: E402
from polar_core import cameras as cm  # noqa: E402

import fakes  # noqa: E402
from demo_data import seed_demo  # noqa: E402
from polar_core import db  # noqa: E402


def no_ros():
    raise ImportError("No module named 'rclpy'")


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
    path = str(tmp_path / "polar.sqlite")
    seed_demo(db.connect(path))
    ctx = fakes.bind(build_context(path, video_options=fakes.video_options(), ros_factory=no_ros))
    w = MainWindow(ctx)
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
    assert f.errors["name"].text() and f.errors["latitude"].text()
    assert f.errors["model_id"].text() == "оберіть модель зі списку"
    assert f.widgets["fov_x_deg"].text() == "—"
    f.widgets["model_id"].setCurrentIndex(f.widgets["model_id"].findText("демо", Qt.MatchFlag.MatchStartsWith))
    assert f.widgets["fov_x_deg"].text() == "60" and f.widgets["fov_y_deg"].text() == "34"   # оптика з моделі
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
    assert new.model == "демо" and new.model_id is not None and new.fov_x_deg == 60
    assert cm.stream_password(win.ctx.conn, win.ctx.box, new.id) == "секрет"
    assert "Камера 4" in win.control._items                  # з'явилась і в «Управлінні»
    assert wait(6, lambda: win.ctx.video.stats("Камера 4") and win.ctx.video.stats("Камера 4").frames > 3)


def test_edit_marks_panorama_stale_and_rename(win):
    rec = win.ctx.records()[0]
    win.edit_camera(rec.id)
    f = win.form
    assert f.widgets["name"].text() == "Камера 1"
    assert f.widgets["model_id"].currentText().startswith("демо")
    from polar_core.camera_models import CameraModelSpec
    win.ctx.save_camera_model(CameraModelSpec("Ширококутна", 65, 40))
    win.edit_camera(rec.id)                                  # список моделей оновлюється при відкритті форми
    w = f.widgets["model_id"]
    w.setCurrentIndex(w.findText("Ширококутна", Qt.MatchFlag.MatchStartsWith))
    assert f.widgets["fov_x_deg"].text() == "65"
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
    assert 120 <= f.widgets["video_latency_ms"].value() <= 250   # 150 мс потоку + час доставки кадру


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
    assert f.widgets["address"].text() == "/dev/v4l/by-id/usb-test_HD_USB_Camera-video-index0"
    assert (f.widgets["stream_res_x"].text(), f.widgets["stream_res_y"].text()) == ("1280", "720")
    assert f.widgets["fps"].value() == 60.0 and f.widgets["codec"].currentData() == "MJPEG"
    f.widgets["model_id"].setCurrentIndex(1)
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


def test_demo_checkboxes_in_form(win):
    win.cameras.add_btn.click()
    f = win.form
    f.widgets["name"].setText("Без обладнання")
    for k, v in (("latitude", "50.1"), ("longitude", "30.1"), ("altitude_m", "100")):
        f.widgets[k].setText(v)
    f.widgets["address"].setText("")
    f.widgets["stream_res_x"].setText("1280")
    f.widgets["stream_res_y"].setText("720")
    assert "address" in f.validate()
    f.widgets["demo_video"].setChecked(True)
    f.widgets["demo_servos"].setChecked(True)
    assert "address" not in f.validate() and f.save_btn.isEnabled()
    assert f.save()
    rec = [r for r in win.ctx.records() if r.name == "Без обладнання"][0]
    assert rec.demo_video and rec.demo_servos
    assert wait(6, lambda: win.ctx.control.status(rec.name).link_ok)       # імітатор сервоприводів
    assert wait(6, lambda: win.ctx.video.stats(rec.name) and win.ctx.video.stats(rec.name).frames > 3)


def test_demo_toggle_on_control_screen(win):
    win.stack.setCurrentWidget(win.control)
    win.control.select_camera("Камера 2")
    c = win.control
    assert c.demo_servos.isChecked() and not c.demo_video.isChecked()
    assert wait(3, lambda: "демо" in c.camera_list.item(1).text())
    c.demo_servos.setChecked(False)                    # без ROS 2 — немає звʼязку і підказка
    assert not win.ctx.camera(win.ctx.control.config("Камера 2").camera_id).demo_servos
    assert wait(4, lambda: "ROS 2 недоступний" in c.error_label.text()), c.error_label.text()
    c.go_btn.setEnabled(True)
    c.go_btn.click()
    assert "ROS 2 недоступний" in c.message.text()
    c.demo_servos.setChecked(True)
    assert wait(4, lambda: win.ctx.control.status("Камера 2").link_ok)
    c.demo_video.setChecked(True)
    assert win.ctx.camera(win.ctx.control.config("Камера 2").camera_id).demo_video
    assert wait(5, lambda: win.ctx.video.latest("Камера 2") is not None)


def test_check_connection_fills_resolution(win):
    win.cameras.add_btn.click()
    f = win.form
    assert "resolution_x" not in f.widgets                     # у «Оптиці» роздільної здатності немає
    assert f.errors["stream_res_x"].text()                # обовʼязкова, поки порожня
    f.widgets["address"].setText("rtsp://10.0.0.20/stream1")
    f.check_btn.click()
    assert wait(15, lambda: "Зʼєднання працює" in f.message.text()), f.message.text()
    assert (f.widgets["stream_res_x"].text(), f.widgets["stream_res_y"].text()) == ("640", "360")
    assert f.errors["stream_res_x"].text() == ""


def test_streams_follow_screens(win):
    v = win.ctx.video
    assert not any(v.is_running(n) for n in ("Камера 1", "Камера 2", "Камера 3"))   # список камер — без відео
    rec = win.ctx.records()[0]
    win.edit_camera(rec.id)
    assert v.is_running(rec.name)
    win.form.cancel_btn.click()                                    # вихід з «Редагування»
    assert not v.is_running(rec.name)
    win.stack.setCurrentWidget(win.control)                        # вхід в «Управління»
    c = win.control
    assert v.is_running(c._current)
    assert wait(6, lambda: not c.preview.pixmap().isNull()), c.video_info.text()
    assert wait(3, lambda: "кадрів/с" in c.video_info.text()), c.video_info.text()
    first = c._current
    c.select_camera("Камера 3")                                    # інша камера — інший потік
    assert v.is_running("Камера 3") and not v.is_running(first)
    win.show_cameras()                                             # вихід з «Управління»
    assert not v.is_running("Камера 3")


def test_camera_menu_has_list_and_control_only(win):
    menu = win.menuBar().actions()[0].menu()
    assert [a.text() for a in menu.actions()] == ["Список", "Управління"]


def test_form_sections_collapse_and_buttons_visible(win):
    win.cameras.add_btn.click()
    f = win.form
    assert set(f.sections) == {"Загальне", "Оптика", "Розміщення", "Відеопотік", "Управління"}
    assert f.sections["Загальне"].is_expanded()
    assert not f.sections["Розміщення"].is_expanded() and not f.sections["Відеопотік"].body.isVisible()
    assert "помил" in f.sections["Розміщення"].header.text()       # помилки видно і в згорнутому блоці
    # кнопки форми — поза прокручуваною частиною, видимі без прокрутки
    assert f.save_btn.isVisible() and f.check_btn.isVisible()
    assert f.save_btn.mapTo(f, f.save_btn.rect().bottomLeft()).y() <= f.height()
    f.sections["Відеопотік"].header.click()                         # розгорнути натисканням
    assert f.sections["Відеопотік"].is_expanded() and f.sections["Відеопотік"].body.isVisible()
    f.sections["Відеопотік"].header.click()                         # згорнути
    assert not f.sections["Відеопотік"].body.isVisible()
    f.expand_errors()
    assert f.sections["Розміщення"].is_expanded()
    for k, v in (("latitude", "50.1"), ("longitude", "30.1"), ("altitude_m", "100")):
        f.widgets[k].setText(v)
    assert f.sections["Розміщення"].header.text() == "Розміщення"


def test_calibration_screen(win):
    from polar_core import calibration as cal

    rec = win.ctx.records()[0]
    win.cameras.table.cellWidget(0, 2).click()                    # «Калібрування» в рядку камери
    c = win.calibration
    assert win.stack.currentWidget() is c and win.ctx.video.is_running(rec.name)
    assert wait(6, lambda: c.view.pixmap is not None and c._img_size is not None)
    assert wait(4, lambda: c.panorama.outline is not None)        # кути датчика є — фігура на панорамі
    w, h = c._img_size
    m = c.model()
    assert (m.cx, m.cy) == (w / 2, h / 2)
    assert any("Кут датчика" in line for line in c.view.text_lines)
    # перетягнути центр і зберегти
    c.view.center_moved.emit(w / 2 + 12, h / 2 - 6)
    c.save_center_btn.click()
    saved = cal.load(win.ctx.conn, rec.id)
    assert (saved.center_x, saved.center_y, saved.frame_w) == (w / 2 + 12, h / 2 - 6, w)
    c.reset_center_btn.click()
    assert c.model().cx == w / 2
    # орієнтир з двох положень камери → похибка розміщення
    c.landmark_btn.setChecked(True)
    assert wait(3, lambda: win.ctx.video.latest(rec.name).has_angles)
    wait(0.3)
    sf = win.ctx.video.latest(rec.name)
    target = c.model().pixel_to_angles(w * 0.7, h * 0.4, sf.angles.azimuth_deg, sf.angles.elevation_deg)
    u, v, _ok = c.model().angles_to_pixel(*target, sf.angles.azimuth_deg, sf.angles.elevation_deg)
    c.view.clicked.emit(u, v)
    c._jog(-1, 0)                                                   # повернути на 10°
    assert wait(4, lambda: abs(((win.ctx.video.latest(rec.name).angles.azimuth_deg - sf.angles.azimuth_deg)
                                 + 180) % 360 - 180) > 9.5 and not win.ctx.video.latest(rec.name).angles.moving)
    wait(0.4)
    sf2 = win.ctx.video.latest(rec.name)
    u2, v2, ok2 = c.model().angles_to_pixel(*target, sf2.angles.azimuth_deg, sf2.angles.elevation_deg)
    assert ok2
    c.view.clicked.emit(u2, v2)
    assert c.check.error() is not None and c.check.error() < 0.5
    assert "в межах" in c.error_label.text()
    c.save_error_btn.click()
    assert cal.load(win.ctx.conn, rec.id).placement_error_deg == pytest.approx(c.check.error())
    c.close_btn.click()
    assert win.stack.currentWidget() is win.cameras and not win.ctx.video.is_running(rec.name)


def test_calibration_tilt_field(win):
    rec = win.ctx.records()[1]
    win.calibrate_camera(rec.id)
    c = win.calibration
    assert c.tilt.value() == rec.tilt_deg and not c.save_tilt_btn.isEnabled()
    c.tilt.setValue(4.25)
    assert c.model().tilt_deg == 4.25 and c.save_tilt_btn.isEnabled()
    assert wait(2, lambda: any("(не збережено)" in line for line in c.view.text_lines))
    assert win.ctx.camera(rec.id).tilt_deg == rec.tilt_deg              # ще не записано
    c.save_tilt_btn.click()
    assert win.ctx.camera(rec.id).tilt_deg == 4.25 and not c.save_tilt_btn.isEnabled()
    assert "збережено" in c.message.text()
    win.edit_camera(rec.id)
    assert win.form.widgets["tilt_deg"].value() == 4.25                 # те саме поле, що в «Редагуванні»


class _Ev:
    """Подія миші для прямого виклику обробників (позиція у віджеті)."""

    def __init__(self, p):
        from PyQt6.QtCore import Qt
        self._p, self._b = p, Qt.MouseButton.LeftButton

    def position(self):
        return self._p

    def button(self):
        return self._b


def test_mask_editor_and_manual_input(win):
    from polar_core.mask import LOWER, UPPER, load_mask

    rec = win.ctx.records()[0]
    win.calibrate_camera(rec.id)
    c = win.calibration
    assert "Панорами ще немає" in c.pano_label.text() or wait(1)
    c.zone_btn.setChecked(True)
    assert c.zone_bar.isVisible()
    pv = c.panorama
    pv.resize(1100, 300)
    c.zone_tools["upper"].click()
    pv.mousePressEvent(_Ev(pv._pt(100, 80)))
    pv.mouseMoveEvent(_Ev(pv._pt(100, 78)))                         # лінію можна перетягнути
    pv.mouseReleaseEvent(_Ev(pv._pt(100, 78)))
    c.zone_tools["lower"].click()
    pv.mousePressEvent(_Ev(pv._pt(10, 3)))
    pv.mouseReleaseEvent(_Ev(pv._pt(10, 3)))
    c.zone_tools["rect"].click()
    pv.mousePressEvent(_Ev(pv._pt(120, 10)))
    pv.mouseMoveEvent(_Ev(pv._pt(150, 25)))
    pv.mouseReleaseEvent(_Ev(pv._pt(150, 25)))
    assert "*" in c.save_mask_btn.text()
    c.save_mask_btn.click()
    m = load_mask(win.ctx.conn, rec.id)
    assert m.limit(UPPER).el == pytest.approx(78, abs=0.5) and m.limit(LOWER).el == pytest.approx(3, abs=0.5)
    r = m.rects()[0]
    assert (r.az_from, r.az_to) == (pytest.approx(120, abs=0.6), pytest.approx(150, abs=0.6))
    c.zone_tools["delete"].click()
    pv.mousePressEvent(_Ev(pv._pt(135, 18)))                       # клік по прямокутнику — видалити
    c.save_mask_btn.click()
    assert len(load_mask(win.ctx.conn, rec.id).rects()) == 0
    win.show_cameras()
    win.calibrate_camera(rec.id)                                    # маска відновлюється
    assert c.mask.limit(UPPER) is not None
    # ручне введення: азимут ↔ кут датчика, фігура за введеними кутами, кадр — у панораму
    assert wait(6, lambda: win.ctx.video.latest(rec.name) is not None)
    c.manual_on.setChecked(True)
    c.m_sensor.setValue(200.0)
    assert c.m_az.value() == pytest.approx((200.0 + rec.north_offset_deg) % 360)
    c.m_el.setValue(30.0)
    c.refresh()
    assert c.panorama.azimuth == pytest.approx((200.0 + rec.north_offset_deg) % 360)
    c.manual_save.click()
    assert "записано" in c.message.text()
    assert wait(2, lambda: c.panorama.pixmap is not None and "покриття" in c.pano_label.text())


def test_work_screen_learning(win):
    win.stack.setCurrentWidget(win.work)
    w = win.work
    assert w.table.rowCount() == 3 and "Очікування" in w.mode.text()
    assert wait(5, lambda: win.ctx.control.status("Камера 1").link_ok)
    w.learn_btn.click()
    assert wait(3, lambda: "Навчання" in w.mode.text())
    assert win.ctx.learning.is_running()
    assert wait(15, lambda: win.ctx.learning.status("Камера 1").step >= 2)
    w.learn_btn.click()                                             # повторне натискання зупиняє
    assert wait(25, lambda: not win.ctx.learning.is_running())
    assert wait(3, lambda: "Очікування" in w.mode.text())
    assert win.ctx.learning.status("Камера 1").state == "stopped"


def test_cameras_grid_and_tracking_button(win):
    names = [r.name for r in win.ctx.records()]
    win.stack.setCurrentWidget(win.grid)
    g = win.grid
    assert set(g.tiles) == set(names) and all(win.ctx.video.is_running(n) for n in names)
    assert wait(6, lambda: all(t.view.pixmap is not None for t in g.tiles.values()))
    assert not g.tiles[names[0]].view.interactive
    win.ctx.control.backend.sim.set_online(names[2], False)
    assert wait(5, lambda: any("Кут місця" in line for line in g.tiles[names[0]].view.text_lines))
    win.show_cameras()
    assert not any(win.ctx.video.is_running(n) for n in names)        # вихід — потоки відключено
    win.stack.setCurrentWidget(win.work)
    win.work.track_btn.click()
    assert "Немає жодної панорами" in win.work.message.text()
    assert not win.ctx.detection.active


def test_cameras_grid_step_panorama_and_save(win):
    """Під кожною камерою — панорама, кнопки кроку на пів кута огляду і «Зберегти»."""
    name = win.ctx.records()[0].name
    win.stack.setCurrentWidget(win.grid)
    g = win.grid
    t = g.tiles[name]
    assert t.panorama.pixmap is None and "панорами ще немає" in t.title.text()
    assert wait(6, lambda: t.view.pixmap is not None and t.btn_up.isEnabled())
    assert wait(5, lambda: t.panorama.outline is not None)            # фігура поля зору на сітці
    t.btn_up.click()
    assert "кут місця" in t.message.text()
    target_el = float(t.message.text().split("кут місця ")[1].split("°")[0])
    assert target_el > 0

    def still():
        sf = win.ctx.video.latest(name)
        return sf is not None and sf.has_angles and not sf.angles.moving and \
            abs(sf.angles.elevation_deg - target_el) < 0.5
    assert wait(15, still)
    time.sleep(0.3)
    t.save_btn.click()
    assert "Збережено" in t.message.text(), t.message.text()
    assert wait(3, lambda: t.panorama.pixmap is not None and "покриття" in t.title.text())
    win.show_cameras()


def test_snapshot_window_follows_camera(win, tmp_path):
    """«Знімок»: окреме вікно зі знімком для поточного положення; поворот камери — інший знімок."""
    name = win.ctx.records()[0].name
    win.stack.setCurrentWidget(win.grid)
    g = win.grid
    t = g.tiles[name]
    assert wait(6, lambda: t.view.pixmap is not None and t.btn_up.isEnabled())

    def still(el):
        sf = win.ctx.video.latest(name)
        return sf is not None and sf.has_angles and not sf.angles.moving and abs(sf.angles.elevation_deg - el) < 0.5

    w = g.open_snapshot(name)
    assert w.isVisible() and w.windowTitle() == f"Знімок · {name}"
    assert wait(5, lambda: "знімка ще немає" in w.info.text())
    paths = []
    for _ in range(2):
        t.btn_up.click()
        el = float(t.message.text().split("кут місця ")[1].split("°")[0])
        assert wait(15, lambda: still(el))
        time.sleep(0.3)
        t.save_btn.click()
        assert "Збережено" in t.message.text(), t.message.text()
        assert wait(3, lambda: w.path is not None and w.path not in paths)
        paths.append(w.path)
    t.btn_down.click()                                            # назад — знову перший знімок
    assert wait(15, lambda: w.path == paths[0])
    assert w.image.pixmap() is not None and not w.image.pixmap().isNull()
    win.show_cameras()
    w.close()


def test_work_screen_targets_table(win):
    """Таблиця виявлених цілей: рядок на камеру; GPS, висота і відстань — для цілі з двох і більше камер."""
    from types import SimpleNamespace as NS

    from polar_core.detection import DetectionSettings
    from polar_core.geo import GeoPoint, LocalFrame, bearing_elevation
    w = win.work
    win.stack.setCurrentWidget(w)
    recs = win.ctx.records()
    w.refresh()
    assert w.targets.rowCount() == len(recs) and w.targets.item(0, 5).text() == ""
    svc = win.ctx.detection.targets
    svc.start(recs, DetectionSettings())
    svc._stop.set()
    svc._thread.join()
    target = GeoPoint(50.4515, 30.5270, 450.0)
    frame = LocalFrame.around(GeoPoint(r.latitude, r.longitude, r.altitude_m) for r in recs)
    for _ in range(3):
        t = svc._clock()
        for r in recs[:2]:                                     # третя камера ціль не бачить
            az, el, _d = bearing_elevation(frame, GeoPoint(r.latitude, r.longitude, r.altitude_m), target)
            svc.offer(r, [(NS(id=1), NS(az=az, el=el))], t)
        svc.offer(recs[2], [], t)
        svc.step()
    win.ctx.detection.mode.start_tracking()                       # таблиця показує цілі в Слідкуванні
    w.refresh()
    assert w.targets.rowCount() == 3
    assert w.targets.item(0, 1).text() == "№1" and w.targets.item(0, 5).text() == "50.451500"
    assert float(w.targets.item(1, 7).text()) == pytest.approx(450.0, abs=0.2)
    assert w.targets.item(2, 0).text() == recs[2].name and w.targets.item(2, 3).text() == ""
    win.ctx.detection.mode.stop()
    svc.stop()


def _kyiv_maps(d):
    """Оглядовий і детальний (у 4 рази дрібніший) GeoTIFF навколо демо-камер (Київ)."""
    import math
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "polar_core", "tests"))
    from geotiff_util import area_raster, write_geotiff
    r = 6.9 / math.cos(math.radians(50.45))
    over, ox, oy = area_raster(50.462, 30.505, 50.438, 30.545, r, seed=1)
    over[..., :3] = (40, 160, 40)                                     # однотонна: легко перевірити пікселі
    det, dx, dy = area_raster(50.4545, 30.520, 50.4465, 30.533, r / 4, seed=2)
    det[..., :3] = (200, 60, 200)
    return (write_geotiff(os.path.join(d, "over.tif"), over, ox, oy, r, description="OpenStreetMap"),
            write_geotiff(os.path.join(d, "detail.tif"), det, dx, dy, r / 4))


def _px(view, x, y):
    """Колір пікселя віджета в логічних координатах (з урахуванням масштабу екрана Windows)."""
    img = view.grab().toImage()
    k = img.devicePixelRatio()
    c = img.pixelColor(int(x * k), int(y * k))
    return c.red(), c.green(), c.blue()


def test_map_settings_levels_and_zoom(win, tmp_path):
    """«Налаштування / Карта»: на кожному рівні своя карта; вибір файлу одразу завантажує рівень;
    зум перемикає рівні й показує карту відповідного рівня."""
    over, det = _kyiv_maps(str(tmp_path))
    menus = {a.text(): a for a in win.menuBar().actions()}
    assert "Налаштування" in menus and "Карта" in [a.text() for a in menus["Налаштування"].menu().actions()]
    assert [a.text() for a in menus["Робота"].menu().actions()][0] == "Карта"
    win.stack.setCurrentWidget(win.map_settings)
    m = win.map_settings
    assert not hasattr(m, "main_file") and not hasattr(m, "load_btn")      # блоку завантаження немає
    m.level_edits[1].setText(det)                                          # номер рівня — довільний
    m.level_edits[1].editingFinished.emit()
    m.level_edits[4].setText(over)
    m.level_edits[4].editingFinished.emit()
    ok = wait(30, lambda: not win.ctx.maps.busy() and "завантажено" in m.message.text().lower())
    assert ok, (m.message.text(), win.ctx.maps.status)
    assert "м/піксель" in m.table.item(0, 3).text() and "м/піксель" in m.table.item(3, 3).text()
    assert m.osm.isChecked() and m.view.attribution == "© OpenStreetMap contributors"
    assert [lv.level for lv in m.view.levels] == [4, 1]                    # від оглядового до детального
    rec = win.ctx.records()[0]
    m.view.resize(600, 400)
    m.view.center_on(rec.latitude, rec.longitude)
    m.view.set_index(1)
    wait(0.2)
    assert "detail.tif" in m.toolbar.label.text() and not m.toolbar.plus.isEnabled()
    assert "Рівень 1 (найбільша деталізація)" in m.toolbar.label.text()
    assert m.table.item(0, 0).text().startswith("1 — найбільша") and m.table.item(7, 0).text().startswith("8 — найменша")
    pt = m.view.to_screen(rec.latitude, rec.longitude)
    assert _px(m.view, pt.x() + 40, pt.y() + 40) == (200, 60, 200)       # детальна карта
    m.toolbar.minus.click()
    wait(0.1)
    assert "over.tif" in m.toolbar.label.text() and not m.toolbar.minus.isEnabled()
    assert "уся карта" in m.toolbar.label.text()
    a, b, c, d = m.view.current.bounds                                     # уся карта рівня — у вигляді
    for x, y in ((a, b), (c, d)):
        sx = m.view.width() / 2 + (x - m.view.cx) / m.view.res
        sy = m.view.height() / 2 - (y - m.view.cy) / m.view.res
        assert -1 <= sx <= m.view.width() + 1 and -1 <= sy <= m.view.height() + 1
    sx = m.view.width() / 2 + (a - m.view.cx) / m.view.res                 # лівий верхній кут карти —
    sy = m.view.height() / 2 - (d - m.view.cy) / m.view.res                # далеко від трикутників камер
    assert _px(m.view, sx + 6, sy + 6) == (40, 160, 40)                    # оглядова карта
    # на детальному рівні поза детальною картою — порожньо (оглядова не збільшується під нею)
    m.toolbar.plus.click()
    wait(0.1)
    pt = m.view.to_screen(rec.latitude, 30.5190)                           # на захід від детальної карти
    assert 0 < pt.x() < m.view.width() and _px(m.view, pt.x(), pt.y()) == (217, 217, 212)
    assert m.view.res >= m.view.current.res * m.view.dpr() * 0.9999       # карта не збільшується
    # файл задано, а карти немає (після оновлення програми) — при вході на екран рівень завантажується сам
    with win.ctx.control._db_lock:
        from polar_core import maps as mp
        mp.delete_level(win.ctx.conn, 4)
    win.stack.setCurrentWidget(win.work)
    win.stack.setCurrentWidget(m)
    assert wait(30, lambda: not win.ctx.maps.busy() and 4 in {lv.level for lv in win.ctx.maps.levels()})
    # прибрати рівень
    m.remove(1)
    assert [lv.level for lv in m.view.levels] == [4] and m.level_edits[1].text() == ""


def test_whole_map_on_least_detailed_level(app):
    """Рівень 8 (найменш детальний): велика карта вписується у вигляд цілком; «+» — назад до камер."""
    from polar_app.ui.map_view import MapView
    from polar_core import maps as mp
    v = MapView()
    v.resize(500, 350)
    big = mp.MapLevel(8, 300.0, 2.0e6, 7.0e6, 5000, 3500, "Ukraine.tiff", "")   # 1500 × 1050 км
    small = mp.MapLevel(1, 2.0, 3.2e6, 6.5e6, 800, 600, "Вереси1.tiff", "")
    cam = mp.merc_to_lonlat(3.2e6 + 800, 6.5e6 - 600)
    v.camera_points = lambda: [(cam[1], cam[0])]
    v.set_levels([small, big])
    assert [lv.level for lv in v.levels] == [8, 1] and v.current.level == 1
    v.zoom_out()
    assert v.current.level == 8 and v.whole
    assert v.res == pytest.approx(max(300.0 * v.dpr(), 300.0 * 5000 / v.width() * 1.03, 300.0 * 3500 / v.height() * 1.03))
    assert (v.cx, v.cy) == pytest.approx((2.0e6 + 750000, 7.0e6 - 525000))       # середина карти
    v.zoom_in()
    assert v.current.level == 1 and (v.cx, v.cy) == pytest.approx(mp.lonlat_to_merc(cam[0], cam[1]))


def test_every_level_whole_and_cameras_outside(app):
    """Кожен рівень — уся карта у вигляді, без збільшення; рівень без камер — порожній."""
    from polar_app.ui.map_view import MapView
    from polar_core import maps as mp
    v = MapView()
    v.resize(500, 350)
    lv1 = mp.MapLevel(1, 2.0, 0.0, 6.0e6, 200, 150, "small.tif", "")             # менша за вигляд
    lv2 = mp.MapLevel(2, 20.0, -20000.0, 6.02e6, 3507, 2480, "a4.tif", "")       # А4 300 dpi — більша
    far = mp.MapLevel(3, 50.0, 1.0e6, 7.0e6, 1000, 1000, "elsewhere.tif", "")     # без камер
    c1, c2 = mp.merc_to_lonlat(100.0, 6.0e6 - 100.0), mp.merc_to_lonlat(300.0, 6.0e6 - 200.0)
    v.camera_points = lambda: [(c1[1], c1[0]), (c2[1], c2[0])]
    v.set_levels([lv1, lv2, far])
    assert v.current.level == 1 and v.level_visible
    assert v.res == pytest.approx(2.0 * v.dpr())                                  # 1:1, не збільшено
    assert (v.cx, v.cy) == pytest.approx((200.0, 6.0e6 - 150.0))                  # менша карта — посередині
    v.zoom_out()
    assert v.current.level == 2
    a, b, c, d = lv2.bounds
    assert v.res == pytest.approx(max(20.0 * v.dpr(), (c - a) / v.width() * 1.03, (d - b) / v.height() * 1.03))
    assert (v.cx, v.cy) == pytest.approx(((a + c) / 2, (b + d) / 2))              # уся карта
    assert "зменшено" in v.level_text
    v.zoom_out()
    assert v.current.level == 3 and not v.level_visible and "поза межами" in v.level_text
    assert v.grab().toImage().pixelColor(5, 150).red() in (217, 196)               # порожньо


def test_level_order_warning():
    from polar_app.ui.map_settings import order_warnings
    from polar_core import maps as mp
    fine = mp.MapLevel(2, 2.0, 0, 0, 10, 10, "a", "")
    coarse = mp.MapLevel(5, 50.0, 0, 0, 10, 10, "b", "")
    assert order_warnings([fine, coarse]) == {}
    bad = order_warnings([mp.MapLevel(2, 50.0, 0, 0, 10, 10, "b", ""), mp.MapLevel(5, 2.0, 0, 0, 10, 10, "a", "")])
    assert 2 in bad and "менш детальна, ніж рівень 5" in bad[2]


def test_work_map_camera_triangles(win, tmp_path):
    """«Робота / Карта»: трикутник з вершиною в камері, кут = HFOV, напрям — азимут, висота 70 px."""
    import math
    from PyQt6.QtCore import QPointF

    from polar_app.ui.work_map import triangle
    tri = triangle(QPointF(100, 100), 90.0, 60.0)                  # на схід, HFOV 60°
    a, b, c = tri[0], tri[1], tri[2]
    assert (a.x(), a.y()) == (100, 100)
    assert (b.x() + c.x()) / 2 == pytest.approx(170.0) and (b.y() + c.y()) / 2 == pytest.approx(100.0)
    assert math.degrees(math.atan2(abs(b.y() - c.y()) / 2, 70)) == pytest.approx(30.0)
    win.stack.setCurrentWidget(win.work_map)
    wm = win.work_map
    assert wait(5, lambda: win.ctx.control.status("Камера 1").link_ok)
    win.ctx.control.go_to("Камера 1", 90.0, 10.0)                   # азимут від півночі 90°
    assert wait(10, lambda: abs((win.ctx.control.status("Камера 1").azimuth_deg or 0) - 90.0) < 0.5)
    rec = win.ctx.records()[0]
    wm.view.resize(600, 400)
    wm.view.center_on(rec.latitude, rec.longitude)
    wait(0.3)
    pt = wm.view.to_screen(rec.latitude, rec.longitude)
    ir, _ig, ib = _px(wm.view, pt.x() + 45, pt.y())                   # усередині трикутника, на схід
    orr, _og, ob = _px(wm.view, pt.x(), pt.y() - 45)                   # на північ — поза трикутником
    assert ib > ir + 30 and abs(ob - orr) < 20


def test_settings_map_shows_triangles(win):
    """«Налаштування / Карта» теж показує камери трикутниками з напрямом (не крапками)."""
    win.stack.setCurrentWidget(win.map_settings)
    m = win.map_settings
    assert wait(5, lambda: win.ctx.control.status("Камера 1").link_ok)
    win.ctx.control.go_to("Камера 1", 90.0, 10.0)                   # на схід
    assert wait(10, lambda: abs((win.ctx.control.status("Камера 1").azimuth_deg or 0) - 90.0) < 0.5)
    rec = win.ctx.records()[0]
    wait(0.4)
    pt = m.view.to_screen(rec.latitude, rec.longitude)
    ir, _ig, ib = _px(m.view, pt.x() + 45, pt.y())                    # усередині трикутника, на схід
    orr, _og, ob = _px(m.view, pt.x(), pt.y() - 45)                   # на північ — поза ним
    assert ib > ir + 30 and abs(ob - orr) < 20


def test_work_map_demo_target(win):
    """«Робота / Карта» → «Демо»: правий клік ставить ціль на середній висоті камер, камери
    повертаються центром на неї."""
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest

    from polar_core.geo import GeoPoint, LocalFrame, bearing_elevation
    win.stack.setCurrentWidget(win.work_map)
    wm = win.work_map
    recs = win.ctx.records()
    assert wait(5, lambda: all(win.ctx.control.status(r.name).link_ok for r in recs))
    wm.view.right_clicked.emit(50.45, 30.52)                          # без «Демо» — нічого
    assert wm.demo_target is None
    wm.demo_btn.click()
    assert wm.demo_btn.isChecked() and "правою кнопкою" in wm.demo_info.text()
    # правий клік мишею по точці на схід від камер
    lat, lon = 50.4505, 30.5400
    pt = wm.view.to_screen(lat, lon)
    QTest.mouseClick(wm.view, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(int(pt.x()), int(pt.y())))
    assert wm.demo_target is not None
    g = wm.demo_target
    cam_alt = sum(r.altitude_m for r in recs) / len(recs)
    assert g.altitude_m == pytest.approx(cam_alt + 100.0)                # за замовчуванням +100 м над камерами
    assert abs(g.latitude - lat) < 0.001 and abs(g.longitude - lon) < 0.001
    assert len(wm.demo_aims) == len(recs) and "азимут" in wm.demo_info.text()
    frame = LocalFrame(g)
    for r in recs:
        az, el, _d = bearing_elevation(frame, GeoPoint(r.latitude, r.longitude, r.altitude_m), g)
        assert wait(15, lambda r=r, az=az: abs(((win.ctx.control.status(r.name).azimuth_deg or 0) - az + 180) % 360
                                               - 180) < 0.5), (r.name, az, win.ctx.control.status(r.name))
    assert all(e > 0.5 for _n, _a, e, _d in wm.demo_aims)                 # ціль у повітрі — кут місця > 0
    # зміна висоти одразу перенацілює камери; перемикач способу відліку зберігає висоту цілі
    wm.demo_alt.setValue(250.0)
    assert wm.demo_target.altitude_m == pytest.approx(cam_alt + 250.0)
    wm.demo_alt_mode.setCurrentIndex(1)
    assert wm.demo_alt.value() == pytest.approx(cam_alt + 250.0)
    assert wm.demo_target.altitude_m == pytest.approx(cam_alt + 250.0)
    wm.demo_alt.setValue(500.0)                                          # над рівнем моря
    assert wm.demo_target.altitude_m == pytest.approx(500.0) and "500.0 м над рівнем моря" in wm.demo_info.text()
    r0 = recs[0]
    _az, el, _d = bearing_elevation(LocalFrame(wm.demo_target), GeoPoint(r0.latitude, r0.longitude, r0.altitude_m),
                                    wm.demo_target)
    assert wait(15, lambda: abs((win.ctx.control.status(r0.name).elevation_deg or 0) - el) < 0.5)
    wm.demo_btn.click()
    assert wm.demo_target is None and wm.demo_info.text() == ""


def test_settings_menu_and_general(win, tmp_path, monkeypatch):
    """«Налаштування»: Загальні, Виявлення, ROS 2, Карта; «Загальні» — база, шлях до панорам, резервна копія."""
    monkeypatch.setenv("POLAR_CONFIG_DIR", str(tmp_path / "config"))
    menus = {a.text(): a for a in win.menuBar().actions()}
    assert [a.text() for a in menus["Налаштування"].menu().actions()] == ["Загальні", "Виявлення", "ROS 2", "Карта"]
    assert [a.text() for a in menus["Робота"].menu().actions()] == ["Карта", "Камери", "Режим роботи", "Журнал"]
    win.stack.setCurrentWidget(win.general)
    g = win.general
    assert g.db_file.text() == win.ctx.settings.db_path() and g.pano_dir.text() == ""
    assert "Зараз:" in g.pano_hint.text()
    g.pano_dir.setText(str(tmp_path / "Панорами"))
    g.save_btn.click()
    assert "Збережено" in g.message.text() and win.ctx.learning.panorama_dir() == str(tmp_path / "Панорами")
    assert g.backup(str(tmp_path / "copy.sqlite")) and os.path.isfile(tmp_path / "copy.sqlite")
    assert "ключ паролів" in g.message.text()
    from PyQt6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    assert g.choose_database(create=True, path=str(tmp_path / "нова.sqlite"))
    from polar_app import app_config
    assert app_config.db_path() == str(tmp_path / "нова.sqlite") and "Наступний запуск" in g.message.text()
    assert "База:" in win.db_label.text()


def test_detection_and_ros_settings_screens(win):
    win.stack.setCurrentWidget(win.detection_settings)
    d = win.detection_settings
    assert d.fields["k_tolerance"].value() == 2.0 and d.fields["confirm_frames"].value() == 3
    d.fields["k_tolerance"].setValue(3.0)
    d.fields["confirm_frames"].setValue(4)
    d.save_btn.click()
    assert "Збережено" in d.message.text()
    s = win.ctx.settings.detection()
    assert s.k_tolerance == 3.0 and s.confirm_frames == 4
    d.fields["k_tolerance"].setValue(5.0)
    d.revert_btn.click()                                                  # «Скасувати зміни»
    assert d.fields["k_tolerance"].value() == 3.0
    win.stack.setCurrentWidget(win.ros_settings)
    r = win.ros_settings
    assert r.prefix.text() == "/polar" and r.rmw.currentData() == "rmw_fastrtps_cpp"
    r.rate.setValue(10.0)
    r.save_btn.click()
    assert "Збережено" in r.message.text() and win.ctx.control.ros.state_rate_hz == 10.0
    r.domain.setValue(7)
    r.save_btn.click()
    assert "Перезапустіть програму" in r.message.text()
    r.discovery_mode.setCurrentIndex(1)
    r.discovery.setText("без порту")
    r.save_btn.click()
    assert "адреса:порт" in r.message.text()


def test_events_screen_status_bar_and_error_dialog(win, tmp_path):
    """«Робота / Журнал»: фільтри, нові події, експорт; рядок стану — останні помилки; помилка — вікно."""
    ctx = win.ctx
    cid = ctx.records()[0].id
    with ctx.control._db_lock:
        with ctx.conn:
            ctx.conn.execute("INSERT INTO events (camera_id, kind, message) VALUES (?, 'motion', 'новий обʼєкт')", (cid,))
    win.stack.setCurrentWidget(win.events)
    e = win.events
    assert e.table.rowCount() >= 1 and e.table.item(0, 2).text() == "Рух"
    with ctx.control._db_lock:
        with ctx.conn:
            ctx.conn.execute("INSERT INTO events (camera_id, kind, message) VALUES (?, 'stream_lost', 'потік втрачено')",
                             (cid,))
    assert wait(3, lambda: e.table.item(0, 2).text() == "Потік втрачено")         # дописується сама
    assert wait(3, lambda: "Потік втрачено" in win.statusBar().currentMessage())  # і в рядку стану
    e.kind.setCurrentIndex(e.kind.findData("motion"))
    assert all(e.table.item(r, 2).text() == "Рух" for r in range(e.table.rowCount()))
    assert e.export(str(tmp_path / "events.csv")) and "Записано" in e.message.text()
    win.error_signal.emit("RuntimeError: тест", "Traceback …")
    wait(0.2)
    assert win.last_error_box.text() == "RuntimeError: тест" and "polar.log" in win.last_error_box.informativeText()
    win.last_error_box.close()
