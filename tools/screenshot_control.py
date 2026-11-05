"""Знімки екранів для перевірки вигляду в CI: керування, список камер, форма камери.
Сервоприводи — імітатор; відео — тестові підміни з polar_app/tests/fakes.py (у CI немає камер)."""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QCoreApplication  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from polar_app.__main__ import build_context  # noqa: E402
from polar_app.ui.main_window import MainWindow  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "polar_app", "tests"))
import fakes  # noqa: E402

out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
os.makedirs(out_dir, exist_ok=True)
app = QApplication([])
from demo_data import seed_demo  # noqa: E402
from polar_core import db  # noqa: E402

path = os.path.join(tempfile.mkdtemp(), "demo.sqlite")
seed_demo(db.connect(path))                       # сервоприводи камер — демо (у CI немає Raspberry Pi)
ctx = fakes.bind(build_context(path, video_options=fakes.video_options()))
win = MainWindow(ctx)
win.resize(1180, 1000)
win.show()


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


pump(1.0)
win.grab().save(os.path.join(out_dir, "cameras.png"))

win.stack.setCurrentWidget(win.control)
ctx.control.backend.sim.set_online("Камера 3", False)
scr = win.control
scr.tgt_az.setValue(75.0)
scr.tgt_el.setValue(25.0)
scr.speed.setValue(40.0)
scr.go_btn.click()
pump(1.0)
win.grab().save(os.path.join(out_dir, "control.png"))

win.edit_camera(ctx.records()[0].id)
pump(2.5)
win.grab().save(os.path.join(out_dir, "camera_form.png"))

win.edit_camera(None)
f = win.form
pump(0.3)
win.grab().save(os.path.join(out_dir, "camera_form_new.png"))
f.widgets["protocol"].setCurrentIndex(f.widgets["protocol"].findData("USB"))
f.usb_btn.click()
pump(1.0)
f.usb_dialog.grab().save(os.path.join(out_dir, "usb_dialog.png"))
f.usb_dialog.accept()
pump(0.3)
win.grab().save(os.path.join(out_dir, "camera_form_usb.png"))
print("saved to", out_dir)
ctx.shutdown()
