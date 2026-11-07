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
    assert f.widgets["model"].text() == "Тест: HD USB Camera"
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
    pt = m.view.to_screen(rec.latitude, rec.longitude)
    assert _px(m.view, pt.x() + 40, pt.y() + 40) == (200, 60, 200)       # детальна карта
    m.toolbar.minus.click()
    wait(0.1)
    assert "over.tif" in m.toolbar.label.text() and not m.toolbar.minus.isEnabled()
    pt = m.view.to_screen(rec.latitude, rec.longitude)
    assert _px(m.view, pt.x() + 40, pt.y() + 40) == (40, 160, 40)        # оглядова карта
    # на детальному рівні поза детальною картою видно оглядову
    m.toolbar.plus.click()
    wait(0.1)
    pt = m.view.to_screen(rec.latitude, 30.5190)                           # на захід від детальної карти
    assert 0 < pt.x() < 600 and _px(m.view, pt.x(), pt.y()) == (40, 160, 40)
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
