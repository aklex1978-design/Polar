"""Мітка часу Pi в кадрі: малювання ↔ читання, стійкість до стискання, вираз geq на Pi, зсув годинників."""
import math
import os
import re

import cv2
import numpy as np
import pytest

from polar_core import timestamp as ts


def frame(w=720, h=480, seed=3):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (h, w, 3), dtype=np.uint8)


@pytest.mark.parametrize("ms", [0, 1, 123456, ts.MOD_MS - 1, 1_794_650_382_457])
def test_draw_and_read(ms):
    img = ts.draw_strip(frame(), ms)
    assert ts.read_strip(img) == ms % ts.MOD_MS


def test_survives_jpeg_and_blur():
    img = ts.draw_strip(frame(), 9_876_543)
    ok, buf = cv2.imencode(".jpg", cv2.GaussianBlur(img, (3, 3), 0), [cv2.IMWRITE_JPEG_QUALITY, 30])
    assert ts.read_strip(cv2.imdecode(buf, cv2.IMREAD_COLOR)) == 9_876_543


def test_no_strip_in_plain_frame():
    assert ts.read_strip(frame()) is None
    assert ts.read_strip(np.full((480, 720, 3), 128, np.uint8)) is None
    assert ts.read_strip(np.zeros((4, 100, 3), np.uint8)) is None


def test_unwrap():
    near = 1_794_650_382_457.0
    v = int(near) % ts.MOD_MS
    assert ts.unwrap(v, near + 500) == int(near)
    assert ts.unwrap((v + 100) % ts.MOD_MS, near) == int(near) + 100


def _geq(expr: str, x: int, t: float) -> float:
    """Обчислити вираз ffmpeg geq у Python (if/lt/gte/mod/floor/pow)."""
    py = expr.replace("\\,", ",").replace("if(", "_if(")
    env = {"_if": lambda c, a, b: a if c else b, "lt": lambda a, b: a < b, "gte": lambda a, b: a >= b,
           "mod": lambda a, b: a - b * math.floor(a / b), "floor": math.floor, "pow": pow, "X": x, "T": t}
    return eval(py, env)  # noqa: S307 — лише наш власний вираз


def test_pi_geq_expression_matches_draw_strip():
    ms = 1_794_650_382_457
    ref = ts.draw_strip(np.zeros((8, ts.WIDTH), np.uint8), ms)[0]
    got = [_geq(ts.GEQ_LUM, x, ms / 1000.0) for x in range(ts.WIDTH)]
    assert got == list(ref)


def test_pi_video_script_uses_same_expression():
    here = os.path.dirname(__file__)
    script = open(os.path.join(here, "..", "..", "deploy", "raspberry", "polar-video.sh"), encoding="utf-8").read()
    m = re.search(r"GEQ_LUM='([^']+)'", script)
    assert m and m.group(1) == ts.GEQ_LUM


def test_offset_uses_shortest_round_trip():
    samples = [(100.000, 100.512, 100.030), (101.000, 101.505, 101.010), (102.000, 102.530, 102.060)]
    off, rtt = ts.estimate_offset(samples)
    assert rtt == pytest.approx(0.010) and off == pytest.approx(0.500)


def test_report_lines():
    r = ts.StageReport(500.0, 5.0, ts.stat([80, 90, 100]), ts.stat([20, 30]), ts.stat([8, 9]), 75)
    text = "\n".join(r.lines())
    assert "+500 мс" in text and "Pi → Polar: 90 мс" in text and "Разом кадр Pi → екран: 115 мс" in text
    empty = ts.StageReport(0.0, 1.0, None, None, None, 40)
    assert "мітки часу в кадрах не знайдено (40 кадрів)" in "\n".join(empty.lines())
