"""Відео камер на сервері: захоплення, привʼязка кожного кадру до кутів енкодерів, контроль затримки,
перевірка зʼєднання і вимірювання затримки відео відносно енкодерів."""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import numpy as np

from polar_core import cameras as cm
from polar_core.angles import wrap180
from polar_core.cameras import CameraRecord
from polar_core.secrets import SecretBox
from polar_core.sync import AngleHistory, LatencyEstimator, LatencyMonitor, StampedFrame, stamp
from polar_core.video import VideoSource, capture_target, ffmpeg_options, opencv_opener, redact

SIM_VIDEO_DELAY_S = 0.15          # імітатор: відео відстає від енкодерів, як справжня IP-камера


@dataclass
class StreamStats:
    connected: bool
    fps: float
    frame_size: Optional[Tuple[int, int]]
    frames: int
    frames_without_angles: int
    latency_p95_ms: Optional[float]     # від експозиції кадру до кадру з кутами
    latency_limit_ms: float
    last_error: str
    reconnects: int

    @property
    def latency_ok(self) -> bool:
        return self.latency_p95_ms is None or self.latency_p95_ms <= self.latency_limit_ms


class _Pipeline:
    """Кадри однієї камери → кути → останній кадр з кутами і статистика."""

    def __init__(self, rec: CameraRecord, source: VideoSource, history: AngleHistory,
                 clock: Callable[[], float]) -> None:
        self.rec = rec
        self.source = source
        self.history = history
        self.monitor = LatencyMonitor(rec.max_detection_latency_ms)
        self.latest: Optional[StampedFrame] = None
        self.subscribers = []             # fn(StampedFrame) — напр. вимірювання затримки
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"stamp-{rec.name}", daemon=True)

    def start(self) -> "_Pipeline":
        self.source.start()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.source.stop()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        last = -1
        while not self._stop.is_set():
            f = self.source.wait_frame(after_index=last, timeout=0.5)
            if f is None:
                continue
            last = f.index
            sf = stamp(f.image, f.t, f.index, self.history, self.rec.video_latency_ms)
            self.latest = sf
            self.monitor.record(self.rec.video_latency_ms + (self._clock() - f.t) * 1000.0, sf.has_angles)
            for fn in list(self.subscribers):
                fn(sf)


class VideoService:
    def __init__(self, control, box: SecretBox, sim: bool = False,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.control = control
        self.conn = control.conn
        self._db_lock = control._db_lock
        self.box = box
        self.sim = sim
        self._clock = clock
        self._histories: Dict[str, AngleHistory] = {}
        self._pipes: Dict[str, _Pipeline] = {}
        self._lock = threading.Lock()
        control.backend.add_state_listener(self._on_state)

    # --- кути з топіка state ----------------------------------------------------
    def history(self, name: str) -> AngleHistory:
        with self._lock:
            h = self._histories.get(name)
            if h is None:
                h = self._histories[name] = AngleHistory()
            return h

    def _on_state(self, name: str, snap) -> None:
        self.history(name).add(snap.received_at, snap.azimuth_deg, snap.elevation_deg, snap.moving)

    # --- потоки -------------------------------------------------------------------
    def records(self):
        with self._db_lock:
            return cm.list_cameras(self.conn)

    def start(self) -> None:
        """(Пере)запустити потоки всіх камер з бази."""
        recs = {r.name: r for r in self.records()}
        for name in list(self._pipes):
            if name not in recs:
                self._pipes.pop(name).stop()
        for name, rec in recs.items():
            self.restart(rec)

    def restart(self, rec: CameraRecord) -> None:
        old = self._pipes.pop(rec.name, None)
        if old:
            old.stop()
        self._pipes[rec.name] = _Pipeline(rec, self._make_source(rec), self.history(rec.name),
                                          self._clock).start()

    def stop_camera(self, name: str) -> None:
        p = self._pipes.pop(name, None)
        if p:
            p.stop()

    def shutdown(self) -> None:
        for name in list(self._pipes):
            self.stop_camera(name)

    def _log(self, camera_id: Optional[int], kind: str, message: str) -> None:
        with self._db_lock:
            with self.conn:
                self.conn.execute("INSERT INTO events (camera_id, kind, message) VALUES (?, ?, ?)",
                                  (camera_id, kind, message))

    def _make_source(self, rec: CameraRecord, angles_at=None) -> VideoSource:
        on_event = (lambda kind, msg: self._log(rec.id, kind, msg)) if rec.id else None
        if self.sim:
            from polar_core.sim import SimCamera, SimVideoCapture, make_scene

            hist = self.history(rec.name)

            def default_angles(t):
                a = hist.at(t)
                return (a.azimuth_deg, a.elevation_deg) if a else None
            w, h = rec.frame_size
            scale = min(1.0, 640 / w)                 # імітатор малює менші кадри — швидше
            cam = SimCamera(make_scene(3600, 900, seed=(rec.id or 0) + 7),
                            resolution=(int(w * scale), int(h * scale)), fov_deg=(rec.fov_x_deg, rec.fov_y_deg))
            fps = rec.fps
            return VideoSource(lambda: SimVideoCapture(cam, angles_at or default_angles, fps=fps,
                                                       delay_s=SIM_VIDEO_DELAY_S),
                               rec.name, on_event=on_event)
        password = ""
        if rec.password is not None:
            password = rec.password
        elif rec.id and rec.has_password:
            with self._db_lock:
                password = cm.stream_password(self.conn, self.box, rec.id)
        opts = ffmpeg_options(rec)

        def open_capture():
            target = capture_target(rec, password)      # ONVIF: адреса потоку питається щоразу
            open_capture.label = redact(target)
            return opencv_opener(target, opts)()
        return VideoSource(open_capture, rec.name, on_event=on_event)

    # --- стан ----------------------------------------------------------------------
    def latest(self, name: str) -> Optional[StampedFrame]:
        p = self._pipes.get(name)
        return p.latest if p else None

    def stats(self, name: str) -> Optional[StreamStats]:
        p = self._pipes.get(name)
        if p is None:
            return None
        s = p.source
        return StreamStats(s.connected, s.fps, s.frame_size, p.monitor.frames, p.monitor.frames_without_angles,
                           p.monitor.percentile(), p.monitor.limit_ms, s.last_error, s.reconnects)

    # --- «Перевірити зʼєднання» --------------------------------------------------------
    def check_connection(self, rec: CameraRecord, seconds: float = 2.0, timeout: float = 15.0) -> Future:
        """Відкрити потік за даними форми (ще не збереженими). Результат: (кадр, (ш, в), кадрів/с)."""
        out: Future = Future()

        def run():
            static = (0.0, 20.0)
            src = self._make_source(rec, angles_at=(lambda t: static) if self.sim else None)
            src.name = f"перевірка {rec.name}"
            src._on_event = lambda kind, msg: None
            src.start()
            try:
                first = src.wait_frame(timeout=timeout)
                if first is None:
                    raise IOError(src.last_error or "кадри не надходять")
                end = time.monotonic() + seconds
                last = first
                while time.monotonic() < end:
                    f = src.wait_frame(after_index=last.index, timeout=2.0)
                    if f is None:
                        break
                    last = f
                n = last.index - first.index
                fps = n / (last.t - first.t) if last.t > first.t else 0.0
                out.set_result((last.image, (last.image.shape[1], last.image.shape[0]), fps))
            except Exception as e:  # noqa: BLE001
                out.set_exception(e)
            finally:
                src.stop()
        threading.Thread(target=run, name="check-connection", daemon=True).start()
        return out

    # --- «Отримати список камер» (USB) ---------------------------------------------------
    def list_usb_cameras(self) -> Future:
        """USB-камери, підключені до сервера. Результат: список polar_core.usb_cameras.UsbCamera."""
        from polar_core.usb_cameras import UsbCamera, VideoMode, list_usb_cameras

        out: Future = Future()
        if self.sim:
            out.set_result([
                UsbCamera("/dev/video0", "Імітатор: HD USB Camera", "/dev/v4l/by-id/usb-sim_HD_USB_Camera-video-index0",
                          [VideoMode(1920, 1080, 30.0, "MJPG"), VideoMode(1280, 720, 60.0, "MJPG"),
                           VideoMode(640, 480, 30.0, "YUYV")]),
                UsbCamera("/dev/video2", "Імітатор: Thermal Cam", "", [VideoMode(640, 512, 25.0, "YUYV")]),
            ])
            return out

        def run():
            try:
                out.set_result(list_usb_cameras())
            except Exception as e:  # noqa: BLE001
                out.set_exception(e)
        threading.Thread(target=run, name="usb-list", daemon=True).start()
        return out

    # --- «Виміряти затримку» -------------------------------------------------------------
    def measure_latency(self, name: str, amplitude_deg: float = 25.0, speed_deg_s: float = 50.0,
                        save: bool = True) -> Future:
        """Повернути камеру туди й назад і знайти затримку відео відносно енкодерів.
        Результат: (затримка, мс; якість 0..1). Зберігається в параметрах камери."""
        out: Future = Future()
        pipe = self._pipes.get(name)
        if pipe is None:
            out.set_exception(KeyError(name))
            return out
        st = self.control.status(name)
        if not st.link_ok:
            out.set_exception(RuntimeError(f"{name}: немає звʼязку з камерою"))
            return out
        if self.control.is_learning(name):
            out.set_exception(RuntimeError(f"{name}: триває Навчання"))
            return out
        rec = pipe.rec

        def run():
            w = pipe.source.frame_size[0] if pipe.source.frame_size else rec.frame_size[0]
            est = LatencyEstimator(rec.fov_x_deg, w)
            frames = []
            pipe.subscribers.append(lambda sf: frames.append((sf.image, sf.t)))
            try:
                az0, el0 = st.azimuth_deg, st.elevation_deg
                t_start = self._clock()
                plan = [(az0 + amplitude_deg, speed_deg_s), (az0 - amplitude_deg * 0.4, speed_deg_s * 1.5),
                        (az0, speed_deg_s * 0.7)]
                time.sleep(0.6)
                prev = az0
                for target, speed in plan:
                    self.control.go_to(name, target % 360.0, el0, speed, source="measure")
                    time.sleep(abs(wrap180(target - prev)) / speed + 0.7)
                    prev = target
                t_end = self._clock()
                pipe.subscribers.clear()
                for img, t in frames:
                    est.add_frame(img, t)
                enc = [s for s in pipe.history.samples() if t_start - 1.5 <= s.t <= t_end]
                lag_s, quality = est.estimate(enc)
                if quality < 0.6:
                    raise ValueError(f"ненадійний результат (кореляція {quality:.2f}) — "
                                     "спрямуйте камеру на ділянку з деталями і повторіть")
                ms = int(round(lag_s * 1000))
                if save and rec.id:
                    with self._db_lock:
                        with self.conn:
                            self.conn.execute("UPDATE cameras SET stream_latency_ms = ? WHERE id = ?", (ms, rec.id))
                    rec.video_latency_ms = ms
                out.set_result((ms, quality))
            except Exception as e:  # noqa: BLE001
                out.set_exception(e)
            finally:
                if pipe.subscribers:
                    pipe.subscribers.clear()
        threading.Thread(target=run, name=f"latency-{name}", daemon=True).start()
        return out


def to_rgb(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return np.repeat(image[:, :, None], 3, axis=2)
    return image[:, :, ::-1].copy()        # BGR (OpenCV) → RGB
