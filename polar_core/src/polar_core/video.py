"""Захоплення відео (RTSP / ONVIF / USB / HTTP) з часовою міткою кожного кадру і перепідключенням."""
from __future__ import annotations

import os
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional, Tuple, Union

import numpy as np

from .cameras import CameraRecord
from .onvif import get_stream_uri


@dataclass
class Frame:
    image: np.ndarray
    t: float            # time.monotonic() у момент отримання кадру сервером
    index: int


def with_credentials(url: str, login: str, password: str) -> str:
    if not login:
        return url
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    cred = urllib.parse.quote(login, safe="") + (":" + urllib.parse.quote(password, safe="") if password else "")
    return urllib.parse.urlunsplit((p.scheme, f"{cred}@{host}", p.path, p.query, p.fragment))


def capture_target(rec: CameraRecord, password: str = "",
                   onvif_lookup: Callable[..., str] = get_stream_uri) -> Union[int, str]:
    """Що передати в cv2.VideoCapture для цієї камери."""
    a = rec.address.strip()
    if rec.protocol == "USB":
        return int(a) if a.isdigit() else a
    if rec.protocol == "ONVIF":
        a = onvif_lookup(a, rec.login, password, rec.stream_kind, rec.transport)
    return with_credentials(a, rec.login, password)


def ffmpeg_options(rec: CameraRecord) -> str:
    """Параметри FFmpeg для OpenCV (змінна OPENCV_FFMPEG_CAPTURE_OPTIONS)."""
    opts = []
    if rec.protocol in ("RTSP", "ONVIF"):
        opts.append(("rtsp_transport", rec.transport.lower()))
    # менший буфер — кадр ближчий у часі до кутів датчика
    opts.append(("max_delay", str(int(rec.buffer_ms) * 1000)))
    if rec.buffer_ms == 0:
        opts += [("fflags", "nobuffer"), ("flags", "low_delay")]
    return "|".join(f"{k};{v}" for k, v in opts)


def redact(target: Union[int, str]) -> str:
    """Адреса для журналу — без пароля."""
    if isinstance(target, int):
        return f"USB {target}"
    p = urllib.parse.urlsplit(target)
    if p.password:
        return target.replace(":" + p.password + "@", ":***@")
    return target


class VideoSource:
    """Потік кадрів однієї камери у фоновому потоці.

    * кожен кадр отримує мітку часу time.monotonic() у момент отримання;
    * тримається лише кілька останніх кадрів (старі кадри для виявлення руху не потрібні);
    * при втраті потоку — перепідключення з наростаючою паузою і запис у журнал через on_event.
    """

    def __init__(self, open_capture: Callable[[], object], name: str = "",
                 on_event: Optional[Callable[[str, str], None]] = None, keep: int = 3,
                 reconnect_s: Tuple[float, float] = (0.5, 10.0), clock: Callable[[], float] = time.monotonic,
                 read_timeout_s: float = 5.0) -> None:
        self._open = open_capture
        self.name = name
        self._on_event = on_event or (lambda kind, msg: None)
        self._frames: Deque[Frame] = deque(maxlen=keep)
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reconnect = reconnect_s
        self._clock = clock
        self._read_timeout_s = read_timeout_s
        self._index = 0
        self._times: Deque[float] = deque(maxlen=50)
        self.connected = False
        self.last_error = ""
        self.reconnects = 0
        self.frame_size: Optional[Tuple[int, int]] = None

    # --- керування ---------------------------------------------------------
    def start(self) -> "VideoSource":
        self._thread = threading.Thread(target=self._run, name=f"video-{self.name}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=5)

    # --- кадри -----------------------------------------------------------------
    def latest(self) -> Optional[Frame]:
        with self._cond:
            return self._frames[-1] if self._frames else None

    def wait_frame(self, after_index: int = -1, timeout: float = 5.0) -> Optional[Frame]:
        """Дочекатися кадру, новішого за after_index."""
        end = time.monotonic() + timeout
        with self._cond:
            while not (self._frames and self._frames[-1].index > after_index):
                left = end - time.monotonic()
                if left <= 0 or self._stop.is_set():
                    return None
                self._cond.wait(left)
            return self._frames[-1]

    @property
    def fps(self) -> float:
        with self._cond:
            if len(self._times) < 2:
                return 0.0
            span = self._times[-1] - self._times[0]
            return (len(self._times) - 1) / span if span > 0 else 0.0

    # --- робочий цикл ----------------------------------------------------------
    def _run(self) -> None:
        delay = self._reconnect[0]
        while not self._stop.is_set():
            cap = None
            try:
                cap = self._open()
                if cap is None or not cap.isOpened():
                    raise IOError("не вдалося відкрити потік")
                ok = self._read_loop(cap)
                if ok:
                    delay = self._reconnect[0]     # потік працював — наступна спроба одразу
            except Exception as e:  # noqa: BLE001 — будь-яка помилка потоку → перепідключення
                self.last_error = str(e)
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:  # noqa: BLE001
                        pass
            if self.connected:
                self.connected = False
                self._on_event("stream_lost", f"{self.name}: потік втрачено ({self.last_error})")
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self._reconnect[1])
            self.reconnects += 1

    def _read_loop(self, cap) -> bool:
        got_any = False
        last_ok = time.monotonic()
        while not self._stop.is_set():
            ok, img = cap.read()
            t = self._clock()
            if not ok or img is None:
                if time.monotonic() - last_ok > self._read_timeout_s or got_any:
                    self.last_error = "кадри не надходять"
                    return got_any
                time.sleep(0.01)
                continue
            last_ok = time.monotonic()
            if not self.connected:
                self.connected = True
                self._on_event("stream_ok", f"{self.name}: потік працює ({img.shape[1]}×{img.shape[0]})")
            got_any = True
            self.frame_size = (img.shape[1], img.shape[0])
            with self._cond:
                self._index += 1
                self._frames.append(Frame(img, t, self._index))
                self._times.append(t)
                self._cond.notify_all()
        return got_any


_OPEN_LOCK = threading.Lock()   # параметри FFmpeg задаються змінною оточення — одна камера за раз


def opencv_opener(target: Union[int, str], options: str = "") -> Callable[[], object]:
    """Фабрика cv2.VideoCapture для VideoSource."""
    def open_capture():
        import cv2

        with _OPEN_LOCK:
            if isinstance(target, str):
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = options
            cap = cv2.VideoCapture(target, cv2.CAP_FFMPEG if isinstance(target, str) else cv2.CAP_ANY)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap
    return open_capture
