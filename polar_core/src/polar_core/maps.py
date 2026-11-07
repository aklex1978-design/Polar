"""Карта (ТЗ, «Налаштування / Карта»): до 8 рівнів, на кожному — своя карта з файлу GeoTIFF.

* Система координат карт — WGS 84 / Pseudo-Mercator (EPSG:3857). Файли в EPSG:3857 беруться без
  перерахунку; файли в географічних координатах (EPSG:4326) перераховуються під час завантаження;
  інші системи — лише якщо встановлено rasterio (GDAL).
* Кожен рівень показується у власному масштабі свого файлу (крок пікселя EPSG:3857 з файлу). Зум
  перемикає рівні від оглядового до найдетальнішого — за роздільною здатністю, а не за номером.
* Рівень зберігається тайлами 256 × 256 у сітці пікселів свого файлу: <каталог карт>/<рівень>/<x>/<y>.png;
  межі, крок пікселя, файл і дата — у таблиці map_levels.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

ORIGIN = 20037508.342789244          # половина довжини екватора в EPSG:3857, м
TILE = 256
LEVELS = 8
MERCATOR_CODES = {3857, 900913, 3785, 102100, 102113}
OSM_ATTRIBUTION = "© OpenStreetMap contributors"


class GeoTiffError(ValueError):
    pass


# --- проєкція ------------------------------------------------------------------------------------

def lonlat_to_merc(lon: float, lat: float) -> Tuple[float, float]:
    lat = max(-85.05112878, min(85.05112878, lat))
    return (math.radians(lon) * 6378137.0,
            math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 6378137.0)


def merc_to_lonlat(x: float, y: float) -> Tuple[float, float]:
    return math.degrees(x / 6378137.0), math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)


def res_at_zoom(z: float) -> float:
    """Крок пікселя EPSG:3857 (м) на рівні зуму вебкарти z."""
    return 2 * ORIGIN / (TILE * 2 ** z)


def zoom_for_res(res: float) -> float:
    return math.log2(2 * ORIGIN / (TILE * res))


def ground_res(res: float, lat: float) -> float:
    """Роздільна здатність на місцевості: крок EPSG:3857 × cos(широти)."""
    return res * math.cos(math.radians(lat))


def global_px(x: float, y: float, z: int) -> Tuple[float, float]:
    """Координати EPSG:3857 → пікселі загальної сітки вебкарти на рівні зуму z."""
    r = res_at_zoom(z)
    return (x + ORIGIN) / r, (ORIGIN - y) / r


def map_center(points: Iterable[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    """Центр карти — середина між крайніми координатами камер: ((min+max)/2 широти, так само довготи)."""
    pts = [(la, lo) for la, lo in points if la is not None and lo is not None]
    if not pts:
        return None
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]
    return (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2


# --- GeoTIFF -----------------------------------------------------------------------------------------

@dataclass
class GeoRaster:
    image: np.ndarray                 # H × W × 4, RGBA
    x0: float                         # лівий верхній кут, EPSG:3857
    y0: float
    res_x: float                      # крок пікселя, м EPSG:3857
    res_y: float
    path: str = ""
    epsg: Optional[int] = None        # система координат файлу (до перерахунку)
    osm: bool = False                 # побудовано з даних OpenStreetMap
    tiled: bool = False

    @property
    def width(self) -> int:
        return self.image.shape[1]

    @property
    def height(self) -> int:
        return self.image.shape[0]

    @property
    def res(self) -> float:
        return (self.res_x + self.res_y) / 2

    @property
    def bounds(self) -> Tuple[float, float, float, float]:
        """(min x, min y, max x, max y) у EPSG:3857."""
        return self.x0, self.y0 - self.height * self.res_y, self.x0 + self.width * self.res_x, self.y0

    def lonlat_bounds(self) -> Tuple[float, float, float, float]:
        """(мін. довгота, мін. широта, макс. довгота, макс. широта)."""
        a, b, c, d = self.bounds
        lo0, la0 = merc_to_lonlat(a, b)
        lo1, la1 = merc_to_lonlat(c, d)
        return lo0, la0, lo1, la1

    def contains(self, lat: float, lon: float) -> bool:
        lo0, la0, lo1, la1 = self.lonlat_bounds()
        return lo0 <= lon <= lo1 and la0 <= lat <= la1

    @property
    def center_lat(self) -> float:
        b = self.lonlat_bounds()
        return (b[1] + b[3]) / 2

    @property
    def ground_res(self) -> float:
        return ground_res(self.res, self.center_lat)

    @property
    def native_zoom(self) -> float:
        """Еквівалент зуму вебкарти (з кроку пікселя EPSG:3857)."""
        return zoom_for_res(self.res)


def _geokeys(raw) -> Dict[int, object]:
    """GeoKeyDirectoryTag (34735): ключ → значення (лише ключі зі значенням у самому записі)."""
    vals = list(raw)
    keys = {}
    if len(vals) < 4:
        return keys
    n = vals[3]
    for i in range(n):
        k, loc, _cnt, off = vals[4 + 4 * i: 8 + 4 * i]
        if loc == 0:
            keys[k] = off
    return keys


def _osm_hint(tags: dict) -> bool:
    text = " ".join(str(tags.get(t, "")) for t in (270, 305, 315, 33432, 42112, 700)).lower()
    return "openstreetmap" in text or "osm" in text.split() or "© osm" in text


def read_geotiff(path: str) -> GeoRaster:
    """Прочитати GeoTIFF: зображення RGBA і привʼязку, перераховану в EPSG:3857."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None            # великі карти — нормальна річ
    try:
        img = Image.open(path)
    except (OSError, ValueError) as e:
        raise GeoTiffError(f"не вдалося відкрити файл: {e}") from e
    tags = {k: img.tag_v2.get(k) for k in img.tag_v2.keys()} if hasattr(img, "tag_v2") else {}
    scale, tie, matrix = tags.get(33550), tags.get(33922), tags.get(34264)
    keys = _geokeys(tags.get(34735) or ())
    if not keys or not ((scale and tie) or matrix):
        raise GeoTiffError("файл без привʼязки до координат (немає тегів GeoTIFF) — такий файл не приймається")
    if matrix and not (scale and tie):
        m = list(matrix)
        if abs(m[1]) > 1e-9 or abs(m[4]) > 1e-9:
            raise GeoTiffError("повернуте зображення (ModelTransformation з поворотом) не підтримується")
        sx, sy, ox, oy = m[0], -m[5], m[3], m[7]
    else:
        sc, tp = list(scale), list(tie)
        sx, sy = sc[0], sc[1]
        ox, oy = tp[3] - tp[0] * sx, tp[4] + tp[1] * sy
    model = keys.get(1024)                    # 1 — проєкційна, 2 — географічна
    pixel_is_point = keys.get(1025) == 2
    if pixel_is_point:                        # привʼязка до центру пікселя → до кута
        ox, oy = ox - sx / 2, oy + sy / 2
    epsg = keys.get(3072) if model == 1 else keys.get(2048, 4326 if model == 2 else None)
    tiled = 322 in tags
    rgba = np.asarray(img.convert("RGBA"))
    osm = _osm_hint(tags)
    if model == 1 and epsg in MERCATOR_CODES:
        return GeoRaster(rgba, ox, oy, sx, sy, path, 3857, osm, tiled)
    if model == 2 and epsg in (4326, 4258, 4269, None, 32767):
        return _from_geographic(rgba, ox, oy, sx, sy, path, epsg or 4326, osm, tiled)
    return _reproject_with_gdal(path, epsg, osm, tiled)


def _from_geographic(rgba, lon0, lat0, dlon, dlat, path, epsg, osm, tiled) -> GeoRaster:
    """Файл у широті/довготі → EPSG:3857 (cv2.remap): по рядках широти — нерівномірний крок."""
    import cv2

    h, w = rgba.shape[:2]
    x0, y0 = lonlat_to_merc(lon0, lat0)
    x1, y1 = lonlat_to_merc(lon0 + w * dlon, lat0 - h * dlat)
    res = (x1 - x0) / w                      # крок по x зберігається
    out_h = max(1, int(round((y0 - y1) / res)))
    ys = y0 - (np.arange(out_h) + 0.5) * res
    lat = np.degrees(2 * np.arctan(np.exp(ys / 6378137.0)) - np.pi / 2)
    rows = ((lat0 - lat) / dlat - 0.5).astype(np.float32)
    map_y = np.repeat(rows[:, None], w, axis=1)
    map_x = np.repeat((np.arange(w, dtype=np.float32))[None, :], out_h, axis=0)
    out = cv2.remap(rgba, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return GeoRaster(out, x0, y0, res, res, path, epsg, osm, tiled)


def _reproject_with_gdal(path, epsg, osm, tiled) -> GeoRaster:
    try:
        import rasterio
        from rasterio.warp import Resampling, calculate_default_transform, reproject
    except ImportError as e:
        raise GeoTiffError(f"система координат EPSG:{epsg} потребує перерахунку — встановіть rasterio (GDAL) "
                           f"або експортуйте карту в EPSG:3857") from e
    with rasterio.open(path) as src:
        tr, w, h = calculate_default_transform(src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
        bands = min(src.count, 4)
        out = np.zeros((4, h, w), np.uint8)
        for b in range(bands):
            reproject(rasterio.band(src, b + 1), out[b], src_transform=src.transform, src_crs=src.crs,
                      dst_transform=tr, dst_crs="EPSG:3857", resampling=Resampling.bilinear)
        if bands < 4:
            if bands == 1:
                out[1], out[2] = out[0], out[0]
            out[3] = np.where(out[:3].any(axis=0), 255, 0)
    return GeoRaster(np.ascontiguousarray(out.transpose(1, 2, 0)), tr.c, tr.f, tr.a, -tr.e, path, epsg, osm, tiled)


# --- рівні ---------------------------------------------------------------------------------------------

class Cancelled(Exception):
    pass


@dataclass
class MapLevel:
    level: int                          # 1…8 — рядок у таблиці рівнів
    res: float                          # крок пікселя EPSG:3857, м
    x0: float                           # лівий верхній кут, EPSG:3857
    y0: float
    width: int                          # розмір у пікселях
    height: int
    source_file: str
    tiles_dir: str
    attribution: str = ""
    loaded_at: str = ""
    warning: str = ""

    @property
    def bounds(self) -> Tuple[float, float, float, float]:
        return self.x0, self.y0 - self.height * self.res, self.x0 + self.width * self.res, self.y0

    @property
    def zoom(self) -> float:
        """Еквівалент зуму вебкарти."""
        return zoom_for_res(self.res)

    def ground_res(self) -> float:
        b = self.bounds
        return ground_res(self.res, merc_to_lonlat(0, (b[1] + b[3]) / 2)[1])

    def tile_path(self, tx: int, ty: int) -> str:
        return os.path.join(self.tiles_dir, str(tx), f"{ty}.png")


def estimate_bytes(r: GeoRaster) -> int:
    """Оцінка обсягу тайлів рівня на диску (як без стискання — із запасом)."""
    return r.width * r.height * 4


def free_bytes(directory: str) -> int:
    d = directory
    while d and not os.path.exists(d):
        d = os.path.dirname(d)
    return shutil.disk_usage(d or ".").free


def build_level(r: GeoRaster, level: int, out_dir: str, attribution: str = "",
                progress: Optional[Callable[[float, str], bool]] = None) -> MapLevel:
    """Записати карту рівня тайлами в її власному масштабі. progress(частка, текст) → False скасовує
    (попередня карта рівня залишається)."""
    import cv2

    work = out_dir + ".building"
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work)
    nx, ny = math.ceil(r.width / TILE), math.ceil(r.height / TILE)
    total = nx * ny
    try:
        k = 0
        for tx in range(nx):
            col = os.path.join(work, str(tx))
            for ty in range(ny):
                tile = r.image[ty * TILE:(ty + 1) * TILE, tx * TILE:(tx + 1) * TILE]
                k += 1
                if tile[..., 3].any():
                    if tile.shape[0] != TILE or tile.shape[1] != TILE:      # крайні тайли — доповнити
                        full = np.zeros((TILE, TILE, 4), np.uint8)
                        full[:tile.shape[0], :tile.shape[1]] = tile
                        tile = full
                    os.makedirs(col, exist_ok=True)
                    cv2.imwrite(os.path.join(col, f"{ty}.png"), cv2.cvtColor(tile, cv2.COLOR_RGBA2BGRA))
                if progress is not None and (k % 16 == 0 or k == total) and \
                        progress(k / total, f"тайл {k} з {total}") is False:
                    raise Cancelled()
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.replace(work, out_dir)
    return MapLevel(level, r.res, r.x0, r.y0, r.width, r.height, r.path, out_dir, attribution,
                    dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))


def remove_level_tiles(out_dir: str) -> None:
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)


# --- база (map_levels) і налаштування ------------------------------------------------------------------

@dataclass
class MapSettings:
    level_files: Dict[int, str] = field(default_factory=dict)
    osm: bool = False                 # карти з даних OpenStreetMap — показувати підпис


def load_map_settings(conn: sqlite3.Connection) -> MapSettings:
    rows = dict(conn.execute("SELECT key, value FROM settings WHERE key LIKE 'map.%'").fetchall())
    s = MapSettings(osm=rows.get("map.osm") == "1")
    for lvl in range(1, LEVELS + 1):
        v = rows.get(f"map.level_file.{lvl}")
        if v:
            s.level_files[lvl] = v
    return s


def save_map_settings(conn: sqlite3.Connection, s: MapSettings) -> None:
    vals = {"map.osm": "1" if s.osm else "0"}
    for lvl in range(1, LEVELS + 1):
        vals[f"map.level_file.{lvl}"] = s.level_files.get(lvl, "")
    with conn:
        for k, v in vals.items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))


def save_level(conn: sqlite3.Connection, lv: MapLevel) -> None:
    b = lv.bounds
    with conn:
        conn.execute("DELETE FROM map_levels WHERE zoom = ?", (lv.level,))
        conn.execute(
            "INSERT INTO map_levels (zoom, scale_m_per_px, file_path, loaded_at, web_zoom, state, min_x, min_y, "
            "max_x, max_y, tiles_dir, attribution, warning) VALUES (?, ?, ?, ?, ?, 'file', ?, ?, ?, ?, ?, ?, ?)",
            (lv.level, lv.res, lv.source_file, lv.loaded_at, int(round(lv.zoom)), *b, lv.tiles_dir,
             lv.attribution, lv.warning))


def delete_level(conn: sqlite3.Connection, level: int) -> None:
    with conn:
        conn.execute("DELETE FROM map_levels WHERE zoom = ?", (level,))


def set_attribution(conn: sqlite3.Connection, text: str) -> None:
    with conn:
        conn.execute("UPDATE map_levels SET attribution = ?", (text,))


def load_levels(conn: sqlite3.Connection) -> List[MapLevel]:
    """Завантажені рівні (лише ті, що мають карту), у порядку номерів."""
    rows = conn.execute("SELECT zoom, scale_m_per_px, min_x, min_y, max_x, max_y, file_path, tiles_dir, "
                        "attribution, loaded_at, warning FROM map_levels WHERE state = 'file' AND min_x IS NOT NULL "
                        "ORDER BY zoom").fetchall()
    out = []
    for r in rows:
        res = r[1]
        w, h = int(round((r[4] - r[2]) / res)), int(round((r[5] - r[3]) / res))
        out.append(MapLevel(r[0], res, r[2], r[5], w, h, r[6] or "", r[7] or "", r[8] or "", r[9] or "",
                            r[10] or ""))
    return out


def tiles_root(panorama_dir: str) -> str:
    """Каталог карт — поруч із шляхом до панорам."""
    return os.path.join(os.path.dirname(os.path.abspath(panorama_dir)), "maps")
