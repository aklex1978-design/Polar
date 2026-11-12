"""Запис і читання зображень з будь-якими шляхами.

cv2.imwrite / cv2.imread на Windows не працюють зі шляхами, де є не-ASCII символи (кирилиця:
«Документи», «Вереси.tiff» …) — мовчки повертають False / None. Тут кодування/декодування робить
OpenCV у памʼяті, а файл пише і читає Python, тож шлях може бути будь-яким.
"""
from __future__ import annotations

import os
from typing import Optional, Sequence

import numpy as np


def imwrite(path: str, image: np.ndarray, params: Sequence[int] = ()) -> bool:
    import cv2

    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, image, list(params))
    if not ok:
        return False
    try:
        with open(path, "wb") as fh:
            fh.write(buf.tobytes())
    except OSError:
        return False
    return True


def imread(path: str, flags: Optional[int] = None) -> Optional[np.ndarray]:
    import cv2

    try:
        data = np.fromfile(path, dtype=np.uint8)
    except (OSError, ValueError):
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR if flags is None else flags)
