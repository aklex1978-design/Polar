"""Запуск: python -m polar_app [--sim] [--demo] [--db шлях]"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

from polar_core import db
from polar_core.control import load_ros_settings
from polar_core.secrets import SecretBox


def build_context(db_path: str, sim: bool, demo: bool):
    from .context import AppContext
    from .control.service import ControlService
    from .demo import seed_demo
    from .video_service import VideoService

    conn = db.connect(db_path, check_same_thread=False)
    if demo:
        seed_demo(conn)
    if sim:
        from .control.sim import SimControlBackend
        backend = SimControlBackend()
    else:
        os.environ.update(load_ros_settings(conn).env())   # до rclpy.init()
        from .control.ros import RosControlBackend
        backend = RosControlBackend()
    control = ControlService(conn, backend)
    control.start()
    video = VideoService(control, SecretBox.for_database(db_path), sim=sim)
    video.start()
    return AppContext(conn, video.box, control, video)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="polar", description="Polar — керування камерами")
    p.add_argument("--db", default="polar.sqlite", help="файл бази SQLite")
    p.add_argument("--sim", action="store_true", help="камери й відео — імітатори замість ROS 2 і потоків")
    p.add_argument("--demo", action="store_true", help="тимчасова база з трьома камерами")
    args = p.parse_args(argv)
    db_path = os.path.join(tempfile.mkdtemp(prefix="polar-demo-"), "polar.sqlite") if args.demo else args.db

    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("Потрібен PyQt6: python3 -m pip install 'polar-app[gui]'", file=sys.stderr)
        return 2
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    ctx = build_context(db_path, args.sim, args.demo)
    win = MainWindow(ctx, "імітатор" if args.sim else "")
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
