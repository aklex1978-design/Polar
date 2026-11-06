"""«Демо відео» для окремої камери: синтетична сцена 360°×90° замість відеопотоку.

Кадри малюються за кутами камери з топіка state (справжніх сервоприводів або імітатора) з
затримкою, як у справжньої IP-камери, тож на демо-камері працюють привʼязка кадрів до кутів
і вимірювання затримки."""
from __future__ import annotations

from typing import Callable, Optional, Tuple

from polar_core.cameras import CameraRecord
from polar_core.projection import CameraModel
from polar_core.sim import SimCamera, SimVideoCapture, make_scene
from polar_core.sync import AngleHistory

DEMO_VIDEO_DELAY_S = 0.15
DEMO_MAX_WIDTH = 640                  # демо-кадри менші — малювати швидше


def demo_opener(rec: CameraRecord, history: Optional[AngleHistory]) -> Callable[[], object]:
    def angles(t) -> Optional[Tuple[float, float]]:
        if history is None or not len(history):
            return (0.0, 20.0)                  # камера ще не на звʼязку — нерухомий кадр
        a = history.at(t)
        return (a.azimuth_deg, a.elevation_deg) if a else None

    w, h = rec.frame_size
    scale = min(1.0, DEMO_MAX_WIDTH / w)
    size = (int(w * scale), int(h * scale))
    # кадр малюється тією ж моделлю камери (квадратні пікселі, f з HFOV), що й проєкція панорами
    cam = SimCamera(make_scene(3600, 900, seed=(rec.id or 0) + 7), resolution=size,
                    model=CameraModel(size[0], size[1], rec.fov_x_deg, rec.fov_y_deg))
    return lambda: SimVideoCapture(cam, angles, fps=rec.fps, delay_s=DEMO_VIDEO_DELAY_S)
