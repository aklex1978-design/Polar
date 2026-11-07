"""Карта (ТЗ, «Налаштування / Карта»): GeoTIFF → 8 рівнів масштабу, тайлами на диску.

* Система координат карт — WGS 84 / Pseudo-Mercator (EPSG:3857). Файли в EPSG:3857 беруться без
  перерахунку; файли в географічних координатах (EPSG:4326) перераховуються під час завантаження;
  інші системи — лише якщо встановлено rasterio (GDAL).
* Рівні 1–8 — це рівні зуму вебкарти Z…Z+7; кожен наступний удвічі детальніший. Тайли 256 × 256
  у загальній сітці вебкарти: <каталог карт>/<рівень>/<x>/<y>.png.
* Джерело рівня: власний файл рівня; інакше — зменшення з найближчого детальнішого файлу; інакше —
  збільшення найближчого дрібнішого рівня. Збільшені рівні на диск не пишуться (нових деталей у них
  немає, а обсяг ріс би вчетверо з кожним рівнем) — їх збільшує на екрані віджет карти.
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


# --- рівні -----------------------------------------------------------------------------------------------

@dataclass
class LevelPlan:
    level: int                        # 1…8
    zoom: int                         # рівень зуму вебкарти Z + level − 1
    state: str                        # file | downsampled | upsampled | none
    source: Optional[str] = None      # файл, з якого будується рівень
    source_level: Optional[int] = None    # для upsampled — який рівень збільшується
    pixels: int = 0                   # пікселів на диску
    warning: str = ""
    bounds: Optional[Tuple[float, float, float, float]] = None

    @property
    def state_text(self) -> str:
        name = os.path.basename(self.source) if self.source else ""
        return {"file": f"з файлу {name}", "downsampled": f"зменшено з {name}",
                "upsampled": f"збільшення рівня {self.source_level} на екрані",
                "none": "немає даних"}[self.state]


def default_base_zoom(rasters: Iterable[GeoRaster]) -> int:
    """Z за замовчуванням: рівень найдрібнішого (оглядового) файлу, округлений до цілого."""
    zs = [r.native_zoom for r in rasters]
    return int(round(min(zs))) if zs else 14


def native_level(r: GeoRaster, base_zoom: int) -> int:
    return int(round(r.native_zoom)) - base_zoom + 1


def plan_levels(base_zoom: int, main: Optional[GeoRaster], level_files: Dict[int, GeoRaster]) -> List[LevelPlan]:
    """Звідки береться кожен рівень (ТЗ «Порядок джерел»)."""
    cands = [(lvl, r) for lvl, r in level_files.items()]
    if main is not None:
        cands.append((native_level(main, base_zoom), main))
    plans = []
    for lvl in range(1, LEVELS + 1):
        z = base_zoom + lvl - 1
        p = LevelPlan(lvl, z, "none")
        own = level_files.get(lvl)
        if own is not None:
            p.state, p.source = "file", own.path
            src = own
            ratio = own.res / res_at_zoom(z)
            if not 0.7 <= ratio <= 1.45:
                p.warning = (f"роздільна здатність файлу {own.ground_res:.2f} м/піксель не відповідає рівню "
                             f"({ground_res(res_at_zoom(z), own.center_lat):.2f} м/піксель)")
        else:
            finer = [(cl, r) for cl, r in cands if cl >= lvl]
            if finer:
                cl, src = min(finer, key=lambda c: c[0])
                p.state, p.source = ("file" if cl == lvl and src is main else "downsampled"), src.path
            else:
                src = None
        if src is not None:
            r = res_at_zoom(z)
            a, b, c, d = src.bounds
            p.bounds = src.bounds
            p.pixels = int(math.ceil((c - a) / r) * math.ceil((d - b) / r))
        plans.append(p)
    last = None
    for p in plans:
        if p.state == "none" and last is not None:
            p.state, p.source_level = "upsampled", last
            p.warning = "деталізації файлів не вистачає — рівень показується збільшеним"
        elif p.state != "none":
            last = p.level
    return plans


def estimate_bytes(plans: Iterable[LevelPlan]) -> int:
    """Оцінка обсягу тайлів на диску (PNG RGBA, із запасом — як без стискання)."""
    return sum(p.pixels * 4 for p in plans if p.state in ("file", "downsampled"))


def free_bytes(directory: str) -> int:
    d = directory
    while d and not os.path.exists(d):
        d = os.path.dirname(d)
    return shutil.disk_usage(d or ".").free


class Cancelled(Exception):
    pass


def build_levels(plans: List[LevelPlan], rasters: Dict[str, GeoRaster], out_dir: str,
                 progress: Optional[Callable[[float, str], bool]] = None) -> List[LevelPlan]:
    """Побудувати тайли рівнів. progress(частка, текст) → False скасовує (каталог повертається як був)."""
    import cv2

    work = out_dir + ".building"
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work)
    todo = [p for p in plans if p.state in ("file", "downsampled")]
    total = sum(p.pixels for p in todo) or 1
    done = 0

    def tick(text: str) -> None:
        if progress is not None and progress(min(1.0, done / total), text) is False:
            raise Cancelled()

    try:
        for p in todo:
            src = rasters[p.source]
            r = res_at_zoom(p.zoom)
            gx0, gy0 = global_px(src.x0, src.y0, p.zoom)
            sx, sy = src.res_x / r, src.res_y / r               # масштаб файл → рівень
            w, h = max(1, int(round(src.width * sx))), max(1, int(round(src.height * sy)))
            interp = cv2.INTER_AREA if sx < 1 else cv2.INTER_LINEAR
            img = cv2.resize(src.image, (w, h), interpolation=interp)
            # зсув до сітки тайлів (дробова частина пікселя — через warpAffine)
            tx0, ty0 = int(gx0 // TILE), int(gy0 // TILE)
            tx1, ty1 = int((gx0 + w) // TILE), int((gy0 + h) // TILE)
            ox, oy = gx0 - tx0 * TILE, gy0 - ty0 * TILE
            canvas_w, canvas_h = (tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE
            m = np.float32([[1, 0, ox], [0, 1, oy]])
            canvas = cv2.warpAffine(img, m, (canvas_w, canvas_h), flags=cv2.INTER_LINEAR,
                                    borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
            del img
            level_dir = os.path.join(work, str(p.level))
            n_tiles = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
            k = 0
            for tx in range(tx0, tx1 + 1):
                col = os.path.join(level_dir, str(tx))
                for ty in range(ty0, ty1 + 1):
                    tile = canvas[(ty - ty0) * TILE:(ty - ty0 + 1) * TILE, (tx - tx0) * TILE:(tx - tx0 + 1) * TILE]
                    k += 1
                    if tile[..., 3].any():
                        os.makedirs(col, exist_ok=True)
                        cv2.imwrite(os.path.join(col, f"{ty}.png"), cv2.cvtColor(tile, cv2.COLOR_RGBA2BGRA))
                    if k % 64 == 0:
                        tick(f"рівень {p.level}: тайл {k} з {n_tiles}")
            done += p.pixels
            tick(f"рівень {p.level} готовий")
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.replace(work, out_dir)
    return plans


# --- база (map_levels) і налаштування ------------------------------------------------------------------

@dataclass
class MapLevel:
    level: int
    zoom: int
    scale_m_per_px: float             # крок пікселя EPSG:3857
    state: str
    source_file: Optional[str]
    source_level: Optional[int]
    bounds: Optional[Tuple[float, float, float, float]]
    tiles_dir: str
    attribution: str = ""
    loaded_at: str = ""
    warning: str = ""


@dataclass
class MapSettings:
    main_file: str = ""
    base_zoom: Optional[int] = None
    level_files: Dict[int, str] = field(default_factory=dict)
    osm: Optional[bool] = None        # None — визначати з файлу


def load_map_settings(conn: sqlite3.Connection) -> MapSettings:
    rows = dict(conn.execute("SELECT key, value FROM settings WHERE key LIKE 'map.%'").fetchall())
    s = MapSettings(main_file=rows.get("map.main_file", ""))
    if rows.get("map.base_zoom"):
        s.base_zoom = int(rows["map.base_zoom"])
    if rows.get("map.osm") in ("0", "1"):
        s.osm = rows["map.osm"] == "1"
    for lvl in range(1, LEVELS + 1):
        v = rows.get(f"map.level_file.{lvl}")
        if v:
            s.level_files[lvl] = v
    return s


def save_map_settings(conn: sqlite3.Connection, s: MapSettings) -> None:
    vals = {"map.main_file": s.main_file, "map.base_zoom": "" if s.base_zoom is None else str(s.base_zoom),
            "map.osm": "" if s.osm is None else ("1" if s.osm else "0")}
    for lvl in range(1, LEVELS + 1):
        vals[f"map.level_file.{lvl}"] = s.level_files.get(lvl, "")
    with conn:
        for k, v in vals.items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))


def save_levels(conn: sqlite3.Connection, plans: List[LevelPlan], tiles_dir: str, attribution: str) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM map_levels")
        for p in plans:
            b = p.bounds or (None, None, None, None)
            conn.execute(
                "INSERT INTO map_levels (zoom, scale_m_per_px, file_path, loaded_at, web_zoom, state, source_level, "
                "min_x, min_y, max_x, max_y, tiles_dir, attribution, warning) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (p.level, res_at_zoom(p.zoom), p.source, now, p.zoom, p.state, p.source_level, *b,
                 os.path.join(tiles_dir, str(p.level)), attribution, p.warning))


def load_levels(conn: sqlite3.Connection) -> List[MapLevel]:
    rows = conn.execute("SELECT zoom, web_zoom, scale_m_per_px, state, file_path, source_level, min_x, min_y, "
                        "max_x, max_y, tiles_dir, attribution, loaded_at, warning FROM map_levels "
                        "ORDER BY zoom").fetchall()
    out = []
    for r in rows:
        b = None if r[6] is None else (r[6], r[7], r[8], r[9])
        out.append(MapLevel(r[0], r[1], r[2], r[3] or "none", r[4], r[5], b, r[10] or "", r[11] or "",
                            r[12] or "", r[13] or ""))
    return out


def tiles_root(panorama_dir: str) -> str:
    """Каталог карт — поруч із шляхом до панорам."""
    return os.path.join(os.path.dirname(os.path.abspath(panorama_dir)), "maps")
