"""Слідкування і Виявлення: кадри всіх камер порівнюються з їхніми панорамами.

Кожна камера обробляється у власному потоці: новий кадр з кутами → (камера стоїть?) → детектор →
супровід обʼєктів → запис у detections (не частіше разу на секунду на обʼєкт). Режим один на систему:
рух на будь-якій камері → «Виявлення»; руху немає на всіх камерах довше за час утримання → «Слідкування».
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np

from polar_core import calibration as cal
from polar_core.detection import (DETECTION, IDLE, MODE_TEXT, TRACKING, DetectionResult, Detector, ModeMachine,
                                  Tracker, load_settings)
from polar_core.mask import load_mask
from polar_core.sync import LatencyMonitor

from .target_service import TargetService


@dataclass
class CameraDetection:
    state: str = "очікує"
    result: Optional[DetectionResult] = None
    frame_index: int = -1
    frame_t: float = 0.0
    tracks: List = field(default_factory=list)
    latency: Optional[LatencyMonitor] = None
    last_reference_update: float = 0.0
    frames: int = 0


class DetectionService:
    SAVE_EVERY_S = 1.0
    SETTLE_S = 0.6          # після зупинки камери кадри ще можуть бути «з руху» (затримка відео, змаз)

    def __init__(self, ctx, clock: Callable[[], float] = time.monotonic) -> None:
        self.ctx = ctx
        self._clock = clock
        with ctx.control._db_lock:
            self.settings = load_settings(ctx.conn)
        self.mode = ModeMachine(self.settings.hold_s)
        self.cams: Dict[str, CameraDetection] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._acquired: List[str] = []
        self.targets = TargetService(ctx, clock)       # координати цілей з обʼєктів усіх камер

    # --- керування ---------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.mode.mode in (TRACKING, DETECTION)

    def mode_text(self) -> str:
        return MODE_TEXT[self.mode.mode]

    def start(self) -> List[str]:
        """Увімкнути Слідкування для камер, що мають панораму. Повертає, які камери стежать."""
        if self.active:
            return list(self._threads)
        with self.ctx.control._db_lock:
            self.settings = load_settings(self.ctx.conn)
        self.mode = ModeMachine(self.settings.hold_s)
        self._stop = threading.Event()
        names = []
        for rec in self.ctx.records():
            if self.ctx.learning.panorama(rec.name) is None:
                self.cams[rec.name] = CameraDetection(state="немає панорами — потрібне Навчання")
                continue
            if self.ctx.video.acquire(rec.name):
                self._acquired.append(rec.name)
            self.cams[rec.name] = CameraDetection(state="стежить",
                                                  latency=LatencyMonitor(rec.max_detection_latency_ms))
            t = threading.Thread(target=self._run, args=(rec,), name=f"detect-{rec.name}", daemon=True)
            self._threads[rec.name] = t
            names.append(rec.name)
        if names:
            self.targets.start(self.ctx.records(), self.settings)
            self.mode.start_tracking()
            self._log(None, "mode", "Слідкування: " + ", ".join(names))
            for t in self._threads.values():
                t.start()
        return names

    def stop(self) -> None:
        self._stop.set()
        for t in list(self._threads.values()):
            t.join(5)
        self._threads.clear()
        self.targets.stop()
        for name in self._acquired:
            self.ctx.video.release(name)
        self._acquired.clear()
        if self.mode.mode != IDLE:
            self._log(None, "mode", "Слідкування вимкнено")
        self.mode.stop()
        for c in self.cams.values():
            c.state = "очікує"

    shutdown = stop

    # --- обробка однієї камери ----------------------------------------------------------------
    def _run(self, rec) -> None:
        name = rec.name
        cam = self.cams[name]
        det = Detector(self.settings)
        tracker = Tracker(gate_deg=max(1.0, 4 * self.settings.min_size_deg), lost_s=max(1.0, self.settings.hold_s))
        model = None
        mask_ver = None
        mask = None
        last_index = -1
        last_move = self._clock()
        while not self._stop.is_set():
            sf = self.ctx.video.latest(name)
            if sf is None or sf.index == last_index:
                time.sleep(0.01)
                self._update_mode()
                continue
            last_index = sf.index
            if self.ctx.learning.is_running(name):
                cam.state = "Навчання"
                continue
            if not sf.has_angles:
                cam.state = "немає кутів (немає звʼязку з сервоприводами)"
                self._update_mode()
                continue
            if sf.angles.moving:
                last_move = sf.t
            if sf.t - last_move < self.SETTLE_S:
                self.targets.clear(name)
                with self._lock:
                    cam.state = "камера рухається"
                    cam.result = None
                self._update_mode()
                continue
            size = (sf.image.shape[1], sf.image.shape[0])
            if model is None or (model.width, model.height) != size:
                with self.ctx.control._db_lock:
                    model = cal.model_for(self.ctx.conn, rec, frame_size=size)
            ver = self.ctx.learning.version.get(name, 0)
            if mask_ver is None or self._clock() - mask_ver > 2.0:      # маску можуть змінити в калібруванні
                with self.ctx.control._db_lock:
                    mask = load_mask(self.ctx.conn, rec.id)
                mask_ver = self._clock()
            pano = self.ctx.learning.panorama(name)
            if pano is None:
                cam.state = "немає панорами"
                continue
            res = det.detect(sf.image, pano, model, sf.angles.azimuth_deg, sf.angles.elevation_deg, mask=mask,
                             moving=sf.angles.moving, pano_key=(id(pano), ver))
            now = self._clock()
            pairs, new = tracker.update(res.objects, sf.t) if res.compared else ([], [])
            if res.compared:
                self.targets.offer(rec, pairs, sf.t)
            else:
                self.targets.clear(name)
            with self._lock:
                cam.result, cam.frame_index, cam.frame_t = res, sf.index, sf.t
                cam.tracks = list(tracker.tracks)
                cam.frames += 1
                cam.state = ("камера рухається" if not res.compared and res.reason == "камера рухається"
                             else (res.reason if not res.compared else
                                   (f"обʼєктів: {len(res.objects)}" if res.objects else "руху немає")))
            cam.latency.record(rec.video_latency_ms + (now - sf.t) * 1000.0, True)
            self._save(rec, pairs, new, sf.t)
            self._maybe_update_reference(rec, cam, res, sf, model)
            self._update_mode()

    def _update_mode(self) -> None:
        now = self._clock()
        with self._lock:
            motion = any(c.result is not None and c.result.objects and now - c.frame_t < 1.0
                         for c in self.cams.values())
            before = self.mode.mode
            after = self.mode.update(motion, now)
        if after != before:
            self._log(None, "mode", f"{MODE_TEXT[before]} → {MODE_TEXT[after]}")

    def _save(self, rec, pairs, new, t) -> None:
        rows = []
        for tr, o in pairs:
            if tr in new or t - tr.last_saved_t >= self.SAVE_EVERY_S:
                tr.last_saved_t = t
                rows.append((rec.id, dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                             o.az, o.el, max(o.w, o.h)))
        if not rows:
            return
        with self.ctx.control._db_lock:
            with self.ctx.conn:
                self.ctx.conn.executemany("INSERT INTO detections (camera_id, ts, azimuth_deg, elevation_deg, size_px) "
                                          "VALUES (?, ?, ?, ?, ?)", rows)
        for tr in new:
            self._log(rec.id, "motion", f"{rec.name}: новий обʼєкт №{tr.id}, азимут {tr.az:.1f}°, кут місця {tr.el:.1f}°")

    def _maybe_update_reference(self, rec, cam, res, sf, model) -> None:
        """Еталон підлаштовується під освітлення: раз на «Частоту оновлення еталона», коли камера стоїть."""
        every = self.settings.reference_update_min * 60.0
        if every <= 0 or not res.compared or self._clock() - cam.last_reference_update < every:
            return
        exclude = np.zeros(sf.image.shape[:2], bool)
        for o in res.objects:
            x0, y0 = int(o.u - o.w), int(o.v - o.h)
            exclude[max(0, y0): int(o.v + o.h) + 1, max(0, x0): int(o.u + o.w) + 1] = True
        self.ctx.learning.update_reference(rec.name, sf.image, model, sf.angles.azimuth_deg,
                                           sf.angles.elevation_deg, exclude)
        cam.last_reference_update = self._clock()

    # --- стан для екранів ----------------------------------------------------------------------
    def camera(self, name: str) -> CameraDetection:
        return self.cams.get(name, CameraDetection())

    def _log(self, camera_id, kind, message) -> None:
        with self.ctx.control._db_lock:
            with self.ctx.conn:
                self.ctx.conn.execute("INSERT INTO events (camera_id, kind, message) VALUES (?, ?, ?)",
                                      (camera_id, kind, message))
