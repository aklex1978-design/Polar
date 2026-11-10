"""Запуск: python -m polar_app [--db шлях]

Без --db відкривається база, запамʼятована в «Налаштування / Загальні» (конфігурація користувача),
інакше polar.sqlite у поточній теці. Помилки — вікном, у журнал і в polar.log поруч із базою.

Демо вмикається окремо для кожної камери (форма камери або екран «Управління»):
«Демо сервоприводів» — імітатор замість Raspberry Pi і ROS 2, «Демо відео» — синтетична сцена замість потоку.
"""
from __future__ import annotations

import argparse
import os
import sys

from polar_core import db
from polar_core.control import load_ros_settings
from polar_core.secrets import SecretBox


def build_context(db_path: str, video_options: dict = None, ros_factory=None):
    from .context import AppContext
    from .control.mixed import MixedControlBackend
    from .control.service import ControlService
    from .video_service import VideoService

    conn = db.connect(db_path, check_same_thread=False)
    os.environ.update(load_ros_settings(conn).env())       # до rclpy.init()
    backend = MixedControlBackend(**({"ros_factory": ros_factory} if ros_factory else {}))
    control = ControlService(conn, backend)
    control.start()
    video = VideoService(control, SecretBox.for_database(db_path), **(video_options or {}))
    # потоки підключаються, коли їх показує екран («Редагування», «Управління»)
    return AppContext(conn, video.box, control, video)


def main(argv=None) -> int:
    from . import app_config
    from .errors import ErrorReporter, log, setup_log_file

    p = argparse.ArgumentParser(prog="polar", description="Polar — керування камерами")
    p.add_argument("--db", default=None,
                   help="файл бази SQLite (без параметра — запамʼятований у «Налаштування / Загальні», "
                        "інакше polar.sqlite у поточній теці)")
    args = p.parse_args(argv)
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("Потрібен PyQt6: python3 -m pip install 'polar-app[gui]'", file=sys.stderr)
        return 2
    from .ui.main_window import RESTART_CODE, MainWindow

    app = QApplication(sys.argv[:1])
    reporter = ErrorReporter()
    reporter.install()
    db_path = app_config.db_path(args.db)
    while True:
        setup_log_file(db_path)
        try:
            ctx = build_context(db_path)
        except Exception as e:  # noqa: BLE001 — оператор бачить причину, а не консоль
            log.exception("не вдалося відкрити базу %s", db_path)
            QMessageBox.critical(None, "Polar", f"Не вдалося відкрити базу\n{db_path}\n\n{e}")
            return 1
        app_config.remember_db(db_path)
        reporter.ctx = ctx
        with ctx.control._db_lock:
            with ctx.conn:
                ctx.conn.execute("INSERT INTO events (camera_id, kind, message) VALUES (NULL, 'start', ?)",
                                 (f"Запуск програми, база {db_path}",))
        win = MainWindow(ctx)
        reporter.on_error = win.error_signal.emit
        win.show()
        code = app.exec()
        reporter.on_error = None
        reporter.ctx = None
        if code != RESTART_CODE and win.exit_code != RESTART_CODE:
            return code
        db_path = app_config.db_path()                    # нова база, обрана в «Загальних»


if __name__ == "__main__":
    sys.exit(main())
