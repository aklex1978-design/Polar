"""Підміни для автоматичних тестів: синтетичний відеопотік і список USB-камер.

У програмі режиму імітації відео немає — камери завжди показують справжній потік. Тут лише
тестові «камери», щоб перевіряти привʼязку кадрів до кутів і вимірювання затримки без обладнання."""
from polar_core.sim import SimCamera, SimVideoCapture, make_scene
from polar_core.usb_cameras import UsbCamera, VideoMode

VIDEO_DELAY_S = 0.15          # відео відстає від енкодерів, як у справжньої IP-камери


class FakeStreams:
    """open_factory для VideoService: кадри з синтетичної сцени за кутами з топіка state."""

    def __init__(self) -> None:
        self.vs = None

    def __call__(self, rec, password):
        def angles(t):
            hist = self.vs.history(rec.name) if self.vs and rec.id else None
            a = hist.at(t) if hist is not None and len(hist) else None
            if a is not None:
                return a.azimuth_deg, a.elevation_deg
            return None if hist is not None and len(hist) else (0.0, 20.0)
        w, h = rec.frame_size
        scale = min(1.0, 640 / w)
        cam = SimCamera(make_scene(3600, 900, seed=(rec.id or 0) + 7),
                        resolution=(int(w * scale), int(h * scale)), fov_deg=(rec.fov_x_deg, rec.fov_y_deg))
        return lambda: SimVideoCapture(cam, angles, fps=rec.fps, delay_s=VIDEO_DELAY_S)


def usb_cameras():
    return [
        UsbCamera("/dev/video0", "Тест: HD USB Camera", "/dev/v4l/by-id/usb-test_HD_USB_Camera-video-index0",
                  [VideoMode(1920, 1080, 30.0, "MJPG"), VideoMode(1280, 720, 60.0, "MJPG"),
                   VideoMode(640, 480, 30.0, "YUYV")]),
        UsbCamera("/dev/video2", "Тест: Thermal Cam", "", [VideoMode(640, 512, 25.0, "YUYV")]),
    ]


def video_options():
    return {"open_factory": FakeStreams(), "usb_lister": usb_cameras}


def bind(ctx_or_vs):
    vs = getattr(ctx_or_vs, "video", ctx_or_vs)
    vs._open_factory.vs = vs
    vs.start()                 # перезапуск потоків уже з привʼязкою до історії кутів
    return ctx_or_vs
