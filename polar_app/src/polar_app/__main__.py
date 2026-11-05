"""Запуск: python -m polar_app [--sim] [--demo] [--db шлях]"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

from polar_core import db
from polar_core.control import load_ros_settings


def build_service(db_path: str, sim: bool, demo: bool):
    from .control.service import ControlService
    from .demo import seed_demo

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
    service = ControlService(conn, backend)
    service.start()
    return service


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="polar", description="Polar — керування камерами")
    p.add_argument("--db", default="polar.sqlite", help="файл бази SQLite")
    p.add_argument("--sim", action="store_true", help="камери-імітатори замість ROS 2")
    p.add_argument("--demo", action="store_true", help="тимчасова база з трьома камерами")
    args = p.parse_args(argv)
    db_path = os.path.join(tempfile.mkdtemp(prefix="polar-demo-"), "polar.sqlite") if args.demo else args.db

    try:
        from PyQt5.QtWidgets import QApplication
    except ImportError:
        print("Потрібен PyQt5: python3 -m pip install 'polar-app[gui]'", file=sys.stderr)
        return 2
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    service = build_service(db_path, args.sim, args.demo)
    win = MainWindow(service, "імітатор" if args.sim else "")
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
