"""Усе, що потрібно екранам: база, ключ паролів, керування камерами і відео."""
from __future__ import annotations

import os
import sqlite3
from typing import Tuple

from polar_core import cameras as cm
from polar_core.cameras import CameraRecord
from polar_core.secrets import SecretBox

from .control.service import ControlService
from .video_service import VideoService


class AppContext:
    def __init__(self, conn: sqlite3.Connection, box: SecretBox, control: ControlService,
                 video: VideoService) -> None:
        self.conn = conn
        self.box = box
        self.control = control
        self.video = video
        from .learning_service import LearningService

        self.learning = LearningService(self)

    def camera(self, camera_id: int) -> CameraRecord:
        with self.control._db_lock:
            return cm.get_camera(self.conn, camera_id)

    def records(self):
        return self.video.records()

    def save_camera(self, rec: CameraRecord) -> Tuple[int, bool]:
        """Зберегти камеру і застосувати: керування (ROS 2) і відеопотік перезапускаються для неї."""
        old_name = None
        if rec.id is not None:
            old_name = self.camera(rec.id).name
        with self.control._db_lock:
            cid, stale = cm.save_camera(self.conn, rec, self.box)
        self.control.reload()
        self.control.start()
        self.video.restart(self.camera(cid), old_name=old_name)
        return cid, stale

    def set_demo(self, camera_id: int, servos=None, video=None) -> None:
        """Демо для однієї камери: імітатор сервоприводів і/або синтетичне відео."""
        with self.control._db_lock:
            cm.set_demo(self.conn, camera_id, servos=servos, video=video)
        rec = self.camera(camera_id)
        if servos is not None:
            try:
                self.control.stop(rec.name)       # справжня камера зупиняється перед переходом у демо
            except Exception:  # noqa: BLE001
                pass
            self.control.reload()
            self.control.start()
        if video is not None:
            self.video.restart(rec)

    def delete_camera(self, camera_id: int) -> None:
        rec = self.camera(camera_id)
        self.video.stop_camera(rec.name)
        try:
            self.control.stop(rec.name)
        except Exception:  # noqa: BLE001 — камера могла бути без звʼязку
            pass
        with self.control._db_lock:
            panorama = cm.delete_camera(self.conn, camera_id)
        self.learning.forget(rec.name)
        if panorama:
            base = os.path.splitext(panorama)[0]
            for f in (panorama, base + ".coverage.png", base + ".json"):
                if os.path.isfile(f):
                    os.remove(f)
        self.control.reload()
        self.control.start()

    def shutdown(self) -> None:
        self.learning.shutdown()
        self.video.shutdown()
        self.control.shutdown()
