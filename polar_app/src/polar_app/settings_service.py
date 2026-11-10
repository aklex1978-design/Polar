"""«Налаштування»: загальні (файл бази, шлях до панорам), «Виявлення», «ROS 2» — збереження в базі
і застосування.

* Шлях до панорам — у settings (files.panorama_dir); нові панорами, знімки й карти пишуться туди.
* Файл бази — у конфігурації користувача (app_config); інша база відкривається після перезапуску.
* «Виявлення» — якщо Слідкування увімкнене, воно перезапускається з новими значеннями.
* «ROS 2» — частота стану, тайм-аут і префікс застосовуються одразу (керування перезапускається);
  ROS_DOMAIN_ID, DDS, інтерфейс, Discovery Server і SROS2 задаються процесу до rclpy.init — після
  перезапуску програми.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import asdict, replace
from typing import List, Tuple

from polar_core import db
from polar_core.control import RosSettings
from polar_core.detection import DetectionSettings, load_settings, save_settings

from . import app_config

RESTART_FIELDS = ("domain_id", "rmw", "interface", "discovery_server", "sros_enabled", "sros_keystore")


class SettingsService:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.started_ros = replace(ctx.control.ros)          # з якими значеннями ROS запущено процес

    # --- загальні -------------------------------------------------------------------------------------
    def db_path(self) -> str:
        with self.ctx.control._db_lock:
            return self.ctx.conn.execute("PRAGMA database_list").fetchone()[2] or ":memory:"

    def panorama_dir(self) -> str:
        return self.ctx.learning.panorama_dir()

    def panorama_dir_is_default(self) -> bool:
        with self.ctx.control._db_lock:
            row = self.ctx.conn.execute("SELECT value FROM settings WHERE key = 'files.panorama_dir'").fetchone()
        return not (row and row[0])

    def set_panorama_dir(self, path: str) -> str:
        """Новий шлях до панорам: тека створюється і перевіряється на запис. Порожньо — поруч із базою."""
        path = path.strip()
        if path:
            path = os.path.abspath(os.path.expanduser(path))
            try:
                os.makedirs(path, exist_ok=True)
                probe = os.path.join(path, ".polar_write_test")
                with open(probe, "w", encoding="utf-8") as fh:
                    fh.write("ok")
                os.remove(probe)
            except OSError as e:
                raise ValueError(f"у теку не можна записувати: {e}") from e
        with self.ctx.control._db_lock:
            with self.ctx.conn:
                self.ctx.conn.execute("INSERT INTO settings (key, value) VALUES ('files.panorama_dir', ?) "
                                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (path,))
        self._log("settings", f"Шлях до панорам: {path or 'поруч із базою (за замовчуванням)'}")
        return self.panorama_dir()

    def check_database(self, path: str, create: bool = False) -> str:
        """Перевірити файл бази (або створити новий) — відкрити і довести схему. Повертає повний шлях."""
        path = os.path.abspath(os.path.expanduser(path))
        if not create and not os.path.isfile(path):
            raise ValueError("файл бази не знайдено")
        if create and os.path.exists(path):
            raise ValueError("файл уже існує — виберіть «Відкрити іншу базу»")
        try:
            conn = db.connect(path)
            conn.execute("SELECT COUNT(*) FROM cameras").fetchone()
            conn.close()
        except (sqlite3.Error, RuntimeError) as e:
            raise ValueError(f"це не база Polar або вона пошкоджена: {e}") from e
        return path

    def switch_database(self, path: str, create: bool = False) -> str:
        """Запамʼятати іншу базу; відкриється після перезапуску програми."""
        path = self.check_database(path, create)
        app_config.remember_db(path)
        self._log("settings", f"Наступний запуск — з бази {path}")
        return path

    def backup(self, dest: str) -> List[str]:
        """Резервна копія бази (SQLite backup — цілісна і під час роботи) разом із ключем паролів."""
        dest = os.path.abspath(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with self.ctx.control._db_lock:
            target = sqlite3.connect(dest)
            try:
                self.ctx.conn.backup(target)
            finally:
                target.close()
        written = [dest]
        key = self.db_path() + ".key"
        if os.path.isfile(key):
            shutil.copyfile(key, dest + ".key")
            written.append(dest + ".key")
        self._log("settings", f"Резервна копія бази: {dest}")
        return written

    # --- виявлення ----------------------------------------------------------------------------------------
    def detection(self) -> DetectionSettings:
        with self.ctx.control._db_lock:
            return load_settings(self.ctx.conn)

    def save_detection(self, s: DetectionSettings) -> str:
        with self.ctx.control._db_lock:
            save_settings(self.ctx.conn, s)                   # ValueError з переліком помилок
        det = self.ctx.detection
        self._log("settings", "Виявлення: " + ", ".join(f"{k}={v}" for k, v in asdict(s).items()))
        if det.active:
            det.stop()
            det.start()
            return "збережено; Слідкування перезапущено з новими значеннями"
        return "збережено; застосується з наступним увімкненням Слідкування"

    # --- ROS 2 ----------------------------------------------------------------------------------------------
    def ros(self) -> RosSettings:
        return replace(self.ctx.control.ros)

    def save_ros(self, s: RosSettings) -> Tuple[str, List[str]]:
        """Зберегти і застосувати. Повертає (повідомлення, поля, що потребують перезапуску програми)."""
        errors = s.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self.ctx.control.save_ros_settings(s)
        self._log("settings", "ROS 2: " + ", ".join(f"{k}={v}" for k, v in asdict(s).items()))
        pending = [f for f in RESTART_FIELDS if getattr(s, f) != getattr(self.started_ros, f)]
        if pending:
            return "збережено; частота стану, тайм-аут і префікс уже діють, решта — після перезапуску", pending
        return "збережено і застосовано", []

    @staticmethod
    def interfaces() -> List[str]:
        """Мережеві інтерфейси цього компʼютера (для вибору інтерфейсу ROS 2)."""
        import socket
        try:
            return sorted({name for _i, name in socket.if_nameindex()})
        except (OSError, AttributeError):
            return []

    def _log(self, kind: str, message: str) -> None:
        with self.ctx.control._db_lock:
            with self.ctx.conn:
                self.ctx.conn.execute("INSERT INTO events (camera_id, kind, message) VALUES (NULL, ?, ?)",
                                      (kind, message))
