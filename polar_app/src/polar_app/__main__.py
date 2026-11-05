"""Запуск: python -m polar_app [--db шлях]

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
    video.start()
    return AppContext(conn, video.box, control, video)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="polar", description="Polar — керування камерами")
    p.add_argument("--db", default="polar.sqlite", help="файл бази SQLite")
    args = p.parse_args(argv)
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("Потрібен PyQt6: python3 -m pip install 'polar-app[gui]'", file=sys.stderr)
        return 2
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    ctx = build_context(args.db)
    win = MainWindow(ctx)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
