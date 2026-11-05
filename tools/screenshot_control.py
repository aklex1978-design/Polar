"""Знімок екрана «Камери / Управління» з імітатором (для перевірки вигляду в CI)."""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QCoreApplication  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from polar_app.__main__ import build_service  # noqa: E402
from polar_app.ui.main_window import MainWindow  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else "control.png"
app = QApplication([])
svc = build_service(os.path.join(tempfile.mkdtemp(), "demo.sqlite"), sim=True, demo=True)
win = MainWindow(svc, "імітатор")
win.resize(1180, 1000)
win.show()
svc.backend.set_online("Камера 3", False)


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


pump(0.5)
scr = win.control
scr.tgt_az.setValue(75.0)
scr.tgt_el.setValue(25.0)
scr.speed.setValue(40.0)
scr.go_btn.click()
pump(1.0)
win.grab().save(out)
print("saved", out)
svc.shutdown()
