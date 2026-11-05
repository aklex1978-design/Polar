"""Знімки екранів з імітатором (для перевірки вигляду в CI): керування, список камер, форма камери."""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QCoreApplication  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from polar_app.__main__ import build_context  # noqa: E402
from polar_app.ui.main_window import MainWindow  # noqa: E402

out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
os.makedirs(out_dir, exist_ok=True)
app = QApplication([])
ctx = build_context(os.path.join(tempfile.mkdtemp(), "demo.sqlite"), sim=True, demo=True)
win = MainWindow(ctx, "імітатор")
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
ctx.control.backend.set_online("Камера 3", False)
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
print("saved to", out_dir)
ctx.shutdown()
