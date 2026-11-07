"""Карта в застосунку: налаштування, перевірка файлів, побудова рівнів у фоні (з ходом і скасуванням)."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from polar_core import maps as mp


@dataclass
class FileInfo:
    path: str
    ok: bool
    text: str                     # роздільна здатність, площа або помилка
    raster: Optional[mp.GeoRaster] = None


@dataclass
class BuildStatus:
    running: bool = False
    fraction: float = 0.0
    text: str = ""
    error: str = ""
    done: bool = False


class MapService:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._rasters: Dict[str, Tuple[float, mp.GeoRaster]] = {}      # шлях → (mtime, растр)
        self.status = BuildStatus()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.version = 0                                                   # росте після кожного завантаження

    # --- налаштування і рівні в базі ---------------------------------------------------------------------
    def settings(self) -> mp.MapSettings:
        with self.ctx.control._db_lock:
            return mp.load_map_settings(self.ctx.conn)

    def save_settings(self, s: mp.MapSettings) -> None:
        with self.ctx.control._db_lock:
            mp.save_map_settings(self.ctx.conn, s)

    def levels(self) -> List[mp.MapLevel]:
        with self.ctx.control._db_lock:
            return mp.load_levels(self.ctx.conn)

    def tiles_dir(self) -> str:
        return mp.tiles_root(self.ctx.learning.panorama_dir())

    def camera_points(self) -> List[Tuple[str, float, float]]:
        return [(r.name, r.latitude, r.longitude) for r in self.ctx.records()
                if r.latitude is not None and r.longitude is not None]

    def center(self) -> Optional[Tuple[float, float]]:
        return mp.map_center((la, lo) for _n, la, lo in self.camera_points())

    # --- файли -----------------------------------------------------------------------------------------------
    def raster(self, path: str) -> mp.GeoRaster:
        mtime = os.path.getmtime(path)
        cached = self._rasters.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        r = mp.read_geotiff(path)
        self._rasters[path] = (mtime, r)
        return r

    def inspect(self, path: str, level_zoom: Optional[int] = None, need_cameras: bool = False) -> FileInfo:
        """Перевірка файлу при виборі: роздільна здатність і площа; попередження про рівень і камери."""
        if not path:
            return FileInfo(path, False, "")
        if not os.path.isfile(path):
            return FileInfo(path, False, "файл не знайдено")
        try:
            r = self.raster(path)
        except (mp.GeoTiffError, OSError, ValueError) as e:
            return FileInfo(path, False, str(e))
        lo0, la0, lo1, la1 = r.lonlat_bounds()
        a, b, c, d = r.bounds
        text = (f"{r.width} × {r.height} пікс., {r.ground_res:.2f} м/піксель (зум ≈ {r.native_zoom:.1f}), "
                f"{(c - a) * mp.math.cos(mp.math.radians(r.center_lat)) / 1000:.1f} × "
                f"{(d - b) * mp.math.cos(mp.math.radians(r.center_lat)) / 1000:.1f} км; "
                f"{la0:.3f}–{la1:.3f}° / {lo0:.3f}–{lo1:.3f}°")
        if r.epsg != 3857:
            text += f"; перераховано з EPSG:{r.epsg}"
        warn = []
        if level_zoom is not None:
            ratio = r.res / mp.res_at_zoom(level_zoom)
            if not 0.7 <= ratio <= 1.45:
                warn.append(f"роздільна здатність не відповідає рівню (зум {level_zoom})")
        if need_cameras:
            outside = [n for n, la, lo in self.camera_points() if not r.contains(la, lo)]
            if outside:
                warn.append("поза межами файлу: " + ", ".join(outside))
        if warn:
            text += " — увага: " + "; ".join(warn)
        return FileInfo(path, True, text, r)

    def plan(self, s: Optional[mp.MapSettings] = None):
        """План рівнів для поточних налаштувань: (Z, плани, помилки)."""
        s = s or self.settings()
        errors = []
        main = None
        if s.main_file:
            info = self.inspect(s.main_file)
            if info.ok:
                main = info.raster
            else:
                errors.append(f"основний файл: {info.text}")
        files = {}
        for lvl, path in s.level_files.items():
            info = self.inspect(path)
            if info.ok:
                files[lvl] = info.raster
            else:
                errors.append(f"рівень {lvl}: {info.text}")
        z = s.base_zoom if s.base_zoom is not None else mp.default_base_zoom(
            ([main] if main else []) + list(files.values()))
        plans = mp.plan_levels(z, main, files) if (main or files) else []
        return z, plans, errors

    # --- побудова -----------------------------------------------------------------------------------------------
    def start_build(self) -> bool:
        if self.status.running:
            return False
        s = self.settings()
        z, plans, errors = self.plan(s)
        if errors or not plans:
            self.status = BuildStatus(error="; ".join(errors) or "не задано жодного файлу карти")
            return False
        self._cancel = threading.Event()
        self.status = BuildStatus(running=True, text="підготовка")
        rasters = {p: self.raster(p) for p in {pl.source for pl in plans if pl.source}}
        osm = s.osm if s.osm is not None else any(r.osm for r in rasters.values())
        self._thread = threading.Thread(target=self._build, args=(plans, rasters, osm), name="map-build",
                                        daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout: Optional[float] = None) -> BuildStatus:
        if self._thread:
            self._thread.join(timeout)
        return self.status

    def _build(self, plans, rasters, osm: bool) -> None:
        st = self.status
        out = self.tiles_dir()

        def progress(f, text):
            st.fraction, st.text = f, text
            return not self._cancel.is_set()
        try:
            mp.build_levels(plans, rasters, out, progress)
            with self.ctx.control._db_lock:
                mp.save_levels(self.ctx.conn, plans, out, mp.OSM_ATTRIBUTION if osm else "")
            st.done, st.fraction, st.text = True, 1.0, "карту завантажено"
            self._log("map_loaded", "Карту завантажено: " + "; ".join(
                f"{p.level}: {p.state_text}" for p in plans))
        except mp.Cancelled:
            st.text = "скасовано — попередня карта залишилась"
        except Exception as e:  # noqa: BLE001
            st.error = str(e)
            st.text = "помилка"
        finally:
            st.running = False
            self.version += 1

    def _log(self, kind: str, message: str) -> None:
        with self.ctx.control._db_lock:
            with self.ctx.conn:
                self.ctx.conn.execute("INSERT INTO events (camera_id, kind, message) VALUES (NULL, ?, ?)",
                                      (kind, message))
