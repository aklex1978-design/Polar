"""Режим Навчання: камери обходять 360° × 90° за планом і записують кадри у свої панорами.

Кожна камера навчається у власному потоці, усі — одночасно. На кожному кроці плану камера стає в
положення, програма чекає зупинки і бере кадр, знятий уже після зупинки; кадр записується в панораму
за фактичними кутами цього кадру (не за цільовими), з центром, дисторсією і нахилом з калібрування.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from polar_core import calibration as cal
from polar_core import cameras as cm
from polar_core.angles import wrap180, wrap360
from polar_core.learning import (aligned_start, az_step, elevation_rows, learning_plan, next_azimuth,
                                 next_row)
from polar_core.panorama import DEFAULT_MAX_PX_PER_DEG, Panorama

from .screenshots import GridSnapshots

IDLE, RUNNING, DONE, STOPPED, ERROR = "idle", "running", "done", "stopped", "error"
STATE_TEXT = {IDLE: "очікує", RUNNING: "навчається", DONE: "завершено", STOPPED: "зупинено", ERROR: "помилка"}


@dataclass
class LearningStatus:
    state: str = IDLE
    step: int = 0
    steps: int = 0
    coverage: float = 0.0
    message: str = ""
    skipped: List[int] = field(default_factory=list)
    snapshots: int = 0                  # знімків на кроках Навчання (крок — половина кута огляду)
    snapshots_dir: str = ""

    @property
    def text(self) -> str:
        return STATE_TEXT[self.state]


class LearningService:
    def __init__(self, ctx, clock: Callable[[], float] = time.monotonic, settle_s: float = 0.25,
                 move_timeout_s: float = 20.0, max_px_per_deg: float = DEFAULT_MAX_PX_PER_DEG) -> None:
        self.ctx = ctx
        self._clock = clock
        self.settle_s = settle_s
        self.move_timeout_s = move_timeout_s
        self.max_px_per_deg = max_px_per_deg
        self._pano: Dict[str, Panorama] = {}
        self.version: Dict[str, int] = {}            # росте після кожного запису кадру (для перемальовування)
        self._status: Dict[str, LearningStatus] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._stop: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._snaps: Dict[str, GridSnapshots] = {}
        self._step_target: Dict[str, tuple] = {}     # остання ціль кнопок кроку: (азимут, кут місця, час)
        self.on_finished: Optional[Callable[[List[str]], None]] = None   # усі камери завершили Навчання
        self._notified = False

    # --- панорами ------------------------------------------------------------------------
    def panorama_dir(self) -> str:
        with self.ctx.control._db_lock:
            row = self.ctx.conn.execute("SELECT value FROM settings WHERE key = 'files.panorama_dir'").fetchone()
            dbfile = self.ctx.conn.execute("PRAGMA database_list").fetchone()[2]
        if row and row[0]:
            return row[0]
        return os.path.join(os.path.dirname(os.path.abspath(dbfile)) if dbfile else os.getcwd(), "panoramas")

    def screenshots_dir(self, rec) -> str:
        return os.path.join(self.panorama_dir(), "screenshots", f"camera_{rec.id}")

    def panorama_path(self, rec) -> str:
        return rec.panorama_path or os.path.join(self.panorama_dir(), f"camera_{rec.id}.png")

    def panorama(self, name: str) -> Optional[Panorama]:
        """Панорама камери: та, що будується зараз, або збережена."""
        with self._lock:
            if name in self._pano:
                return self._pano[name]
        rec = self._rec(name)
        if rec is None or not Panorama.exists(rec.panorama_path):
            return None
        try:
            p = Panorama.load(rec.panorama_path)
        except IOError:
            return None
        with self._lock:
            self._pano.setdefault(name, p)
            return self._pano[name]

    def forget(self, name: str) -> None:
        with self._lock:
            self._pano.pop(name, None)

    def _rec(self, name: str):
        return next((r for r in self.ctx.records() if r.name == name), None)

    def _save(self, rec, pano: Panorama) -> str:
        path = self.panorama_path(rec)
        pano.save(path)
        with self.ctx.control._db_lock:
            with self.ctx.conn:
                self.ctx.conn.execute("UPDATE cameras SET panorama_path = ?, panorama_stale = 0 WHERE id = ?",
                                      (path, rec.id))
        return path

    def _bump(self, name: str) -> None:
        with self._lock:
            self.version[name] = self.version.get(name, 0) + 1

    # --- стан -------------------------------------------------------------------------------
    def status(self, name: str) -> LearningStatus:
        return self._status.get(name, LearningStatus())

    def running(self) -> List[str]:
        return [n for n, s in self._status.items() if s.state == RUNNING]

    def is_running(self, name: Optional[str] = None) -> bool:
        return bool(self.running()) if name is None else self.status(name).state == RUNNING

    # --- запуск і зупинка ---------------------------------------------------------------------
    def start(self, names: Optional[List[str]] = None) -> List[str]:
        """Почати Навчання камер (усіх на звʼязку, якщо names не задано). Повертає, які запущено."""
        started = []
        for rec in self.ctx.records():
            if names is not None and rec.name not in names:
                continue
            if self.is_running(rec.name):
                continue
            st = self.ctx.control.status(rec.name)
            if not st.link_ok:
                self._status[rec.name] = LearningStatus(ERROR, message="немає звʼязку з камерою")
                continue
            stop = threading.Event()
            self._notified = False
            self._stop[rec.name] = stop
            self._status[rec.name] = LearningStatus(RUNNING, message="підготовка")
            self.ctx.control.set_learning(rec.name, True)       # ручне керування блокується одразу
            t = threading.Thread(target=self._run, args=(rec, stop), name=f"learn-{rec.name}", daemon=True)
            self._threads[rec.name] = t
            t.start()
            started.append(rec.name)
        return started

    def stop(self, names: Optional[List[str]] = None) -> None:
        for name, ev in list(self._stop.items()):
            if names is None or name in names:
                ev.set()

    def wait(self, name: str, timeout: Optional[float] = None) -> LearningStatus:
        t = self._threads.get(name)
        if t:
            t.join(timeout)
        return self.status(name)

    def shutdown(self) -> None:
        self.stop()
        for t in list(self._threads.values()):
            t.join(5)

    # --- один прохід камери ---------------------------------------------------------------------
    def _run(self, rec, stop: threading.Event) -> None:
        name = rec.name
        st = self._status[name]
        ctl, video = self.ctx.control, self.ctx.video
        acquired = video.acquire(name)
        try:
            first = self._wait_frame(name, stop, timeout=15.0)
            if first is None:
                raise RuntimeError("немає кадрів з камери")
            size = (first.image.shape[1], first.image.shape[0])
            model, plan, rows = self._grid(rec, size)
            snaps = self._snapshots(rec, rows)
            snaps.reset()
            self._snaps[name] = snaps
            st.snapshots_dir = snaps.directory
            pano = Panorama.for_camera(size[0], rec.fov_x_deg, self.max_px_per_deg)
            with self._lock:
                self._pano[name] = pano
            st.steps = len(plan)
            for i, (az, el) in enumerate(plan):
                if stop.is_set() or not ctl.is_learning(name):
                    st.state, st.message = STOPPED, "зупинено оператором"
                    break
                st.step, st.message = i + 1, f"крок {i + 1} з {len(plan)}: датчик {az:.0f}°, кут місця {el:.0f}°"
                ctl.go_to(name, wrap360(az + rec.north_offset_deg), el, 0.0, source="learning")
                sf = self._wait_still_frame(name, az, el, stop)
                if sf is None:
                    if stop.is_set() or not ctl.is_learning(name):
                        continue                         # зупинку обробить початок циклу
                    st.skipped.append(i)                     # не дійшла за тайм-аут — крок пропущено
                    continue
                pano.add_frame(sf.image, model, sf.angles.azimuth_deg, sf.angles.elevation_deg)
                snaps.offer(sf)
                st.snapshots = snaps.count
                st.coverage = pano.coverage()
                self._bump(name)
            else:
                st.state = DONE
                st.message = (f"панорама готова, покриття {st.coverage * 100:.1f} %; знімків: {st.snapshots}"
                              + (f"; пропущено кроків: {len(st.skipped)}" if st.skipped else ""))
            if pano.coverage() > 0:
                path = self._save(rec, pano)
                self._log(rec.id, "learning_done" if st.state == DONE else "learning_stopped",
                          f"{name}: {st.message}; {path}")
        except Exception as e:  # noqa: BLE001
            st.state, st.message = ERROR, str(e)
            self._log(rec.id, "learning_error", f"{name}: {e}")
        finally:
            ctl.set_learning(name, False)
            if acquired:
                video.release(name)
            self._notify_if_all_done()

    def _grid(self, rec, size):
        """Модель камери, план Навчання і ряди кута місця для кадру розміру size."""
        import math

        with self.ctx.control._db_lock:
            model = cal.model_for(self.ctx.conn, rec, frame_size=size)
        cfg = self.ctx.control.config(rec.name)
        el_lo = max(0.0, cfg.tilt.min_deg if cfg.tilt.min_deg is not None else 0.0)
        el_hi = min(90.0, cfg.tilt.max_deg if cfg.tilt.max_deg is not None else 90.0)
        # фактичний вертикальний кут огляду кадру (за фокусною відстанню з HFOV і висотою кадру):
        # якщо він менший за VFOV з параметрів, крок за кутом місця береться з нього — без пропусків
        vfov = min(rec.fov_y_deg, math.degrees(2 * math.atan(size[1] / 2 / model.f)))
        # кроки по азимуту від півночі кратні кроку HFOV/2 — на кожному кроці зберігається знімок
        plan = learning_plan(rec.fov_x_deg, vfov, el_lo, el_hi,
                             az_start=aligned_start(rec.north_offset_deg, rec.fov_x_deg))
        return model, plan, elevation_rows(vfov, el_lo, el_hi)

    def _snapshots(self, rec, rows) -> GridSnapshots:
        return GridSnapshots(self.screenshots_dir(rec), rec.north_offset_deg, az_step(rec.fov_x_deg), rows)

    # --- «Робота / Обʼєкти управління»: крок камери і ручне збереження ------------------------------------------
    def _pose(self, name: str, rec):
        """Поточні (азимут від півночі, кут місця) і розмір кадру."""
        sf = self.ctx.video.latest(name)
        size = (sf.image.shape[1], sf.image.shape[0]) if sf is not None else rec.frame_size
        if sf is not None and sf.has_angles:
            return wrap360(sf.angles.azimuth_deg + rec.north_offset_deg), sf.angles.elevation_deg, size
        st = self.ctx.control.status(name)
        if st.link_ok and st.azimuth_deg is not None:
            return wrap360(st.azimuth_deg), st.elevation_deg, size
        raise RuntimeError(f"{name}: немає кутів камери (немає звʼязку з сервоприводами)")

    def snapshot_for(self, name: str):
        """Збережений знімок для поточного положення камери: (повний шлях, запис index.json, азимут від
        півночі, кут місця). Без кутів — None; без знімка для цього положення — (None, None, азимут, місце)."""
        from .screenshots import nearest_snapshot

        rec = self._rec(name)
        if rec is None:
            return None
        try:
            az, el, _size = self._pose(name, rec)
        except RuntimeError:
            return None
        found = nearest_snapshot(self.screenshots_dir(rec), az, el)
        return (found[0], found[1], az, el) if found else (None, None, az, el)

    def step(self, name: str, d_az: int, d_el: int):
        """Повернути камеру на сусідній вузол сітки Навчання: по азимуту на HFOV/2, по куту місця на
        VFOV/2 (вузли ті самі, що й у Навчання, тож знімки «Зберегти» лягають на вузли).
        Повертає ціль (азимут від півночі, кут місця)."""
        rec = self._rec(name)
        if rec is None:
            raise KeyError(name)
        if self.is_running(name):
            raise RuntimeError(f"{name}: триває Навчання")
        az, el, size = self._pose(name, rec)
        last = self._step_target.get(name)
        if last is not None and self._clock() - last[2] < 5.0 and \
                (abs(wrap180(az - last[0])) > 0.5 or abs(el - last[1]) > 0.5):
            az, el = last[0], last[1]            # камера ще йде до попередньої цілі — крок від цілі
        _model, _plan, rows = self._grid(rec, size)
        if d_el:
            el = next_row(rows, el, d_el)
        if d_az:
            az = next_azimuth(az, az_step(rec.fov_x_deg), d_az)
        target = self.ctx.control.go_to(name, az, el, 0.0)
        self._step_target[name] = (target[0], target[1], self._clock())
        return target

    def save_current(self, name: str):
        """Кнопка «Зберегти»: поточний кадр нерухомої камери — у панораму (за фактичними кутами кадру) і
        в знімки. Повертає (покриття, шлях знімка)."""
        rec = self._rec(name)
        if rec is None:
            raise KeyError(name)
        if self.is_running(name):
            raise RuntimeError(f"{name}: триває Навчання — кадри записуються автоматично")
        sf = self.ctx.video.latest(name)
        if sf is None:
            raise RuntimeError(f"{name}: немає кадру камери")
        if not sf.has_angles:
            raise RuntimeError(f"{name}: немає кутів кадру (немає звʼязку з сервоприводами)")
        if sf.angles.moving:
            raise RuntimeError(f"{name}: камера рухається — дочекайтесь зупинки")
        size = (sf.image.shape[1], sf.image.shape[0])
        model, _plan, rows = self._grid(rec, size)
        pano = self.panorama(name) or Panorama.for_camera(size[0], rec.fov_x_deg, self.max_px_per_deg)
        with self._lock:
            self._pano[name] = pano
        pano.add_frame(sf.image, model, sf.angles.azimuth_deg, sf.angles.elevation_deg)
        self._bump(name)
        self._save(rec, pano)
        snaps = self._snapshots(rec, rows)
        snaps.load()
        path = snaps.save(sf)
        az = wrap360(sf.angles.azimuth_deg + rec.north_offset_deg)
        self._log(rec.id, "manual_save", f"{name}: кадр записано (азимут {az:.1f}°, кут місця "
                                          f"{sf.angles.elevation_deg:.1f}°); {path}")
        return pano.coverage(), path

    def _notify_if_all_done(self) -> None:
        with self._lock:
            if any(s.state == RUNNING for s in self._status.values()) or self._notified:
                return
            done = [n for n, s in self._status.items() if s.state == DONE]
            self._notified = True                       # одне повідомлення на запуск Навчання
        if done and self.on_finished:
            self.on_finished(done)

    def _wait_frame(self, name, stop, timeout):
        end = self._clock() + timeout
        while self._clock() < end and not stop.is_set():
            sf = self.ctx.video.latest(name)
            if sf is not None and sf.has_angles:
                return sf
            time.sleep(0.05)
        return None

    def _wait_still_frame(self, name, az, el, stop):
        """Кадр, знятий після того, як камера зупинилась біля цілі."""
        ctl, video = self.ctx.control, self.ctx.video
        end = self._clock() + self.move_timeout_s
        reached = None
        while self._clock() < end and not stop.is_set() and ctl.is_learning(name):
            snap = ctl.backend.snapshot(name)
            if snap is not None and not snap.moving and abs(wrap180(snap.azimuth_deg - az)) < 1.0 \
                    and abs(snap.elevation_deg - el) < 1.0:
                if reached is None:
                    reached = self._clock()
                sf = video.latest(name)
                if sf is not None and sf.has_angles and not sf.angles.moving \
                        and sf.angles.t_angles >= reached + self.settle_s \
                        and abs(wrap180(sf.angles.azimuth_deg - az)) < 1.0:
                    return sf
            else:
                reached = None
            time.sleep(0.03)
        return None

    def update_reference(self, name: str, image, model, sensor_az: float, el: float, exclude=None,
                         alpha: float = 0.3) -> None:
        """Оновлення еталона під час Слідкування (освітлення змінилось); файл перезаписується."""
        pano = self.panorama(name)
        if pano is None:
            return
        pano.add_frame(image, model, sensor_az, el, alpha=alpha, exclude=exclude)
        self._bump(name)
        rec = self._rec(name)
        if rec is not None:
            self._save(rec, pano)

    # --- ручне введення (калібрування) -------------------------------------------------------------
    def add_manual(self, name: str, image, sensor_az: float, el: float) -> float:
        """Записати кадр у панораму за введеними вручну кутами. Повертає покриття."""
        rec = self._rec(name)
        if rec is None:
            raise KeyError(name)
        size = (image.shape[1], image.shape[0])
        with self.ctx.control._db_lock:
            model = cal.model_for(self.ctx.conn, rec, frame_size=size)
        pano = self.panorama(name) or Panorama.for_camera(size[0], rec.fov_x_deg, self.max_px_per_deg)
        with self._lock:
            self._pano[name] = pano
        pano.add_frame(image, model, sensor_az, el)
        self._bump(name)
        self._save(rec, pano)
        return pano.coverage()

    def _log(self, camera_id, kind, message) -> None:
        with self.ctx.control._db_lock:
            with self.ctx.conn:
                self.ctx.conn.execute("INSERT INTO events (camera_id, kind, message) VALUES (?, ?, ?)",
                                      (camera_id, kind, message))
