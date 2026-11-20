"""Карта в застосунку: рівні 1–8, на кожному — своя карта з файлу GeoTIFF.

Вибір файлу для рівня одразу запускає завантаження цього рівня у фоні (читання, перерахунок у
EPSG:3857, запис тайлів); хід видно в рядку рівня, завантаження можна скасувати — тоді лишається
попередня карта рівня. «Прибрати» видаляє карту рівня.
"""
from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from polar_core import maps as mp


@dataclass
class FileInfo:
    path: str
    ok: bool
    text: str                     # роздільна здатність, площа або помилка
    raster: Optional[mp.GeoRaster] = None


@dataclass
class LevelStatus:
    running: bool = False
    queued: bool = False
    fraction: float = 0.0
    text: str = ""
    error: str = ""


@dataclass
class _Job:
    level: int
    path: str
    cancel: threading.Event = field(default_factory=threading.Event)


class MapService:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.status: Dict[int, LevelStatus] = {lvl: LevelStatus() for lvl in range(1, mp.LEVELS + 1)}
        self.version = 0                                     # росте після кожної зміни рівнів
        self._jobs: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._current: Optional[_Job] = None
        self._pending: Dict[int, _Job] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    # --- налаштування і рівні в базі -----------------------------------------------------------------------
    def settings(self) -> mp.MapSettings:
        with self.ctx.control._db_lock:
            return mp.load_map_settings(self.ctx.conn)

    def save_settings(self, s: mp.MapSettings) -> None:
        with self.ctx.control._db_lock:
            mp.save_map_settings(self.ctx.conn, s)

    def levels(self) -> List[mp.MapLevel]:
        with self.ctx.control._db_lock:
            return mp.load_levels(self.ctx.conn)

    def level(self, n: int) -> Optional[mp.MapLevel]:
        return next((lv for lv in self.levels() if lv.level == n), None)

    def tiles_dir(self, level: Optional[int] = None) -> str:
        root = mp.tiles_root(self.ctx.learning.panorama_dir())
        return root if level is None else os.path.join(root, str(level))

    def camera_points(self) -> List[Tuple[str, float, float]]:
        return [(r.name, r.latitude, r.longitude) for r in self.ctx.active_records()
                if r.latitude is not None and r.longitude is not None]

    def center(self) -> Optional[Tuple[float, float]]:
        return mp.map_center((la, lo) for _n, la, lo in self.camera_points())

    def set_osm(self, on: bool) -> None:
        s = self.settings()
        s.osm = on
        self.save_settings(s)
        with self.ctx.control._db_lock:
            mp.set_attribution(self.ctx.conn, mp.OSM_ATTRIBUTION if on else "")
        self.version += 1

    # --- файли -------------------------------------------------------------------------------------------------
    def describe(self, r: mp.GeoRaster) -> str:
        lo0, la0, lo1, la1 = r.lonlat_bounds()
        a, b, c, d = r.bounds
        k = mp.math.cos(mp.math.radians(r.center_lat)) / 1000
        text = (f"{r.width} × {r.height} пікс., {r.ground_res:.2f} м/піксель (зум ≈ {r.native_zoom:.1f}), "
                f"{(c - a) * k:.1f} × {(d - b) * k:.1f} км; {la0:.3f}–{la1:.3f}° / {lo0:.3f}–{lo1:.3f}°")
        if r.epsg != 3857:
            text += f"; перераховано з EPSG:{r.epsg}"
        outside = [n for n, la, lo in self.camera_points() if not r.contains(la, lo)]
        if outside:
            text += " — поза картою: " + ", ".join(outside)
        return text

    def inspect(self, path: str) -> FileInfo:
        if not path:
            return FileInfo(path, False, "")
        if not os.path.isfile(path):
            return FileInfo(path, False, "файл не знайдено")
        try:
            r = mp.read_geotiff(path)
        except (mp.GeoTiffError, OSError, ValueError) as e:
            return FileInfo(path, False, str(e))
        return FileInfo(path, True, self.describe(r), r)

    # --- завантаження рівнів -------------------------------------------------------------------------------------
    def load_level(self, level: int, path: str) -> None:
        """Задати файл рівня і завантажити його у фоні (попереднє завантаження цього рівня скасовується)."""
        s = self.settings()
        s.level_files[level] = path
        self.save_settings(s)
        with self._lock:
            old = self._pending.pop(level, None)
            if old:
                old.cancel.set()
            if self._current and self._current.level == level:
                self._current.cancel.set()
            job = _Job(level, path)
            self._pending[level] = job
            self.status[level] = LevelStatus(queued=True, text="у черзі")
        self._jobs.put(job)
        self._ensure_worker()

    def remove_level(self, level: int) -> None:
        self.cancel(level)
        s = self.settings()
        s.level_files.pop(level, None)
        self.save_settings(s)
        with self.ctx.control._db_lock:
            mp.delete_level(self.ctx.conn, level)
        mp.remove_level_tiles(self.tiles_dir(level))
        self.status[level] = LevelStatus()
        self.version += 1

    def cancel(self, level: Optional[int] = None) -> None:
        with self._lock:
            for lvl, job in list(self._pending.items()):
                if level is None or lvl == level:
                    job.cancel.set()
            if self._current and (level is None or self._current.level == level):
                self._current.cancel.set()

    def busy(self) -> bool:
        return any(st.running or st.queued for st in self.status.values())

    def wait(self, timeout: float = 60.0) -> None:
        import time
        end = time.monotonic() + timeout
        while self.busy() and time.monotonic() < end:
            time.sleep(0.02)

    def _ensure_worker(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, name="map-levels", daemon=True)
            self._thread.start()

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            with self._lock:
                if self._pending.get(job.level) is job:
                    del self._pending[job.level]
                if job.cancel.is_set():
                    if self.status[job.level].queued:
                        self.status[job.level] = LevelStatus(text="скасовано")
                    continue
                self._current = job
            self._run(job)
            with self._lock:
                self._current = None

    def _run(self, job: _Job) -> None:
        st = LevelStatus(running=True, text="читання файлу")
        self.status[job.level] = st
        try:
            r = mp.read_geotiff(job.path)
            if job.cancel.is_set():
                raise mp.Cancelled()
            st.text = "запис тайлів"
            osm = self.settings().osm or r.osm
            if r.osm and not self.settings().osm:
                s = self.settings()
                s.osm = True
                self.save_settings(s)

            def progress(f, text):
                st.fraction, st.text = f, text
                return not job.cancel.is_set()
            lv = mp.build_level(r, job.level, self.tiles_dir(job.level),
                                mp.OSM_ATTRIBUTION if osm else "", progress)
            outside = [n for n, la, lo in self.camera_points() if not r.contains(la, lo)]
            if outside:
                lv.warning = "поза картою: " + ", ".join(outside)
            with self.ctx.control._db_lock:
                mp.save_level(self.ctx.conn, lv)
                if osm:
                    mp.set_attribution(self.ctx.conn, mp.OSM_ATTRIBUTION)
            self.status[job.level] = LevelStatus(text="завантажено")
            self._log("map_loaded", f"Рівень {job.level}: {os.path.basename(job.path)} — {self.describe(r)}")
        except mp.Cancelled:
            self.status[job.level] = LevelStatus(text="скасовано — лишилась попередня карта рівня")
        except Exception as e:  # noqa: BLE001
            self.status[job.level] = LevelStatus(error=str(e), text="помилка")
        finally:
            self.version += 1

    def shutdown(self) -> None:
        self.cancel()
        self._jobs.put(None)

    def _log(self, kind: str, message: str) -> None:
        with self.ctx.control._db_lock:
            with self.ctx.conn:
                self.ctx.conn.execute("INSERT INTO events (camera_id, kind, message) VALUES (NULL, ?, ?)",
                                      (kind, message))
