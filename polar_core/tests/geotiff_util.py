"""Синтетичні GeoTIFF для тестів (Pillow, LZW, теги GeoTIFF) — як прикладові файли замовника."""
import numpy as np
from PIL import Image, TiffImagePlugin

from polar_core.maps import lonlat_to_merc


def write_geotiff(path, rgba: np.ndarray, x0: float, y0: float, res: float, epsg: int = 3857,
                  geographic: bool = False, georef: bool = True, description: str = "", res_y=None) -> str:
    img = Image.fromarray(rgba, "RGBA")
    ifd = TiffImagePlugin.ImageFileDirectory_v2()
    if georef:
        ry = res if res_y is None else res_y
        ifd[33550] = (float(res), float(ry), 0.0)
        ifd.tagtype[33550] = 12
        ifd[33922] = (0.0, 0.0, 0.0, float(x0), float(y0), 0.0)
        ifd.tagtype[33922] = 12
        if geographic:
            keys = [1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, epsg]
        else:
            keys = [1, 1, 0, 3, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, epsg]
        ifd[34735] = tuple(keys)
        ifd.tagtype[34735] = 3
    if description:
        ifd[270] = description
    img.save(path, compression="tiff_lzw", tiffinfo=ifd)
    return path


def area_raster(lat0, lon0, lat1, lon1, res, seed=0, marks=()):
    """RGBA-зображення ділянки (широти lat0 > lat1 зверху вниз) з кроком res (EPSG:3857) і мітками
    marks = [(lat, lon, (r, g, b))] — квадрат 5 × 5 пікселів."""
    x0, y0 = lonlat_to_merc(lon0, lat0)
    x1, y1 = lonlat_to_merc(lon1, lat1)
    w, h = int(round((x1 - x0) / res)), int(round((y0 - y1) / res))
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 4), np.uint8)
    img[..., :3] = rng.integers(60, 200, (h, w, 3), dtype=np.uint8) // 8 * 8
    img[..., 3] = 255
    for lat, lon, color in marks:
        mx, my = lonlat_to_merc(lon, lat)
        c, r = int((mx - x0) / res), int((y0 - my) / res)
        img[max(0, r - 2):r + 3, max(0, c - 2):c + 3, :3] = color
    return img, x0, y0
