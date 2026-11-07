"""Координати цілей: обʼєкти, знайдені камерами, → промені → зіставлення за правилом Б → цілі.

Кожна камера після кожного обробленого кадру передає сюди свої обʼєкти (номер треку, азимут від
півночі, кут місця, час кадру). Окремий потік раз на тривалість кадру бере останні обʼєкти всіх камер,
будує промені від положень камер (широта, довгота, висота), розвʼязує зіставлення і веде цілі:
підтверджена ціль (пара тримається задану кількість кадрів поспіль) записується в таблицю targets,
а її координати оновлюються; ціль, якої не бачили довше за час утримання, позначається «lost».
"""
from __future__ import annotations

import datetime as dt
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Hashable, List, Optional, Tuple

from polar_core.geo import GeoPoint, LocalFrame
from polar_core.triangulation import Confirmation, MatchParams, Target, make_ray, solve


@dataclass
class Observation:
    camera: str
    camera_id: int
    track: Hashable
    az: float              # від півночі
    el: float
    t: float               # час кадру (той самий годинник, що й у відео)
    wall: dt.datetime      # час кадру для таблиці


@dataclass
class TrackedTarget:
    number: int                              # номер цілі на екрані
    db_id: Optional[int]
    target: Target
    first_seen: dt.datetime
    last_seen: dt.datetime
    last_t: float
    keys: set = field(default_factory=set)   # (камера, трек), що входили в ціль
    saved_t: float = -1e9

    @property
    def geo(self) -> GeoPoint:
        return self.target.geo


class TargetService:
    SAVE_EVERY_S = 1.0
    FRESH_S = 1.0               # обʼєкти камери старші за це — камера ціль більше не бачить

    def __init__(self, ctx, clock: Callable[[], float] = time.monotonic) -> None:
        self.ctx = ctx
        self._clock = clock
        self._lock = threading.Lock()
        self._obs: Dict[str, Tuple[float, List[Observation]]] = {}
        self._positions: Dict[str, GeoPoint] = {}
        self.frame: Optional[LocalFrame] = None
        self.params = MatchParams()
        self.confirm = Confirmation(3)
        self.lost_s = 5.0
        self.targets: Dict[int, TrackedTarget] = {}
        self.candidates: List[Target] = []          # усі цілі останнього розвʼязку (і не підтверджені)
        self._next_number = 1
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.missing_position: List[str] = []

    # --- запуск ---------------------------------------------------------------------------------
    def start(self, records, settings) -> None:
        self.stop()
        self._positions = {}
        self.missing_position = []
        for rec in records:
            if rec.latitude is None or rec.longitude is None or rec.altitude_m is None:
                self.missing_position.append(rec.name)
                continue
            self._positions[rec.name] = GeoPoint(rec.latitude, rec.longitude, rec.altitude_m)
        self.frame = LocalFrame.around(self._positions.values()) if self._positions else None
        fps = min((rec.fps for rec in records if rec.fps and rec.fps > 0), default=25.0)
        self.params = MatchParams.from_settings(settings, fps=fps)
        self.confirm = Confirmation(self.params.confirm_frames)
        self.lost_s = max(1.0, settings.hold_s)
        with self._lock:
            self._obs.clear()
            self.targets.clear()
            self.candidates = []
        self._stop = threading.Event()
        self._period = 1.0 / max(fps, 1.0)
        self._thread = threading.Thread(target=self._loop, name="targets", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None
        with self._lock:
            for tt in self.targets.values():
                self._write(tt, status="lost")
            self.targets.clear()
            self.candidates = []
            self._obs.clear()

    shutdown = stop

    # --- дані від камер ------------------------------------------------------------------------------
    def offer(self, rec, pairs, t: float) -> None:
        """Обʼєкти цього кадру камери (пари трек–обʼєкт з Tracker). Порожній список — обʼєктів немає."""
        wall = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=max(0.0, self._clock() - t))
        obs = [Observation(rec.name, rec.id, tr.id, o.az, o.el, t, wall) for tr, o in pairs]
        with self._lock:
            self._obs[rec.name] = (t, obs)

    def clear(self, name: str) -> None:
        """Камера рухається або не порівнює — її обʼєктів зараз немає."""
        with self._lock:
            self._obs.pop(name, None)

    # --- розвʼязок ---------------------------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.wait(self._period):
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 — потік цілей не має падати від одного кадру
                self._log(None, "targets_error", str(e))

    def step(self) -> List[TrackedTarget]:
        now = self._clock()
        with self._lock:
            if self.frame is None:
                return []
            rays = []
            for name, (t, obs) in self._obs.items():
                pos = self._positions.get(name)
                if pos is None or now - t > self.FRESH_S:
                    continue
                for o in obs:
                    rays.append(make_ray(self.frame, pos, name, o.track, o.az, o.el, o.t))
            found = solve(rays, self.frame, self.params) if rays else []
            self.confirm.update(found)
            self.candidates = found
            for tgt in found:
                if tgt.confirmed:
                    self._attach(tgt, now)
            for num, tt in list(self.targets.items()):
                if now - tt.last_t > self.lost_s:
                    self._write(tt, status="lost")
                    self._log(None, "target_lost", f"ціль №{num} зникла")
                    del self.targets[num]
            return list(self.targets.values())

    def _attach(self, tgt: Target, now: float) -> None:
        keys = {r.key for r in tgt.rays}
        tt = next((x for x in self.targets.values() if x.keys & keys), None)
        wall = max(self._wall(r) for r in tgt.rays)
        if tt is None:
            tt = TrackedTarget(self._next_number, None, tgt, wall, wall, now, set(keys))
            self._next_number += 1
            self.targets[tt.number] = tt
            self._write(tt)
            tt.saved_t = now
            g = tgt.geo
            self._log(None, "target", f"ціль №{tt.number}: {g.latitude:.6f}, {g.longitude:.6f}, висота "
                                      f"{g.altitude_m:.0f} м; камери: {', '.join(tgt.cameras)}")
        else:
            tt.target, tt.last_seen, tt.last_t = tgt, wall, now
            tt.keys |= keys
            if now - tt.saved_t >= self.SAVE_EVERY_S:
                self._write(tt)
                tt.saved_t = now

    def _wall(self, ray) -> dt.datetime:
        _t, obs = self._obs.get(ray.camera, (0.0, []))
        o = next((x for x in obs if x.track == ray.obj), None)
        return o.wall if o else dt.datetime.now(dt.timezone.utc)

    def _write(self, tt: TrackedTarget, status: str = "active") -> None:
        g, t = tt.geo, tt.target
        vals = (tt.first_seen.isoformat(timespec="milliseconds"), tt.last_seen.isoformat(timespec="milliseconds"),
                g.latitude, g.longitude, g.altitude_m, t.distance_m, t.error_m, len(t.rays), status)
        with self.ctx.control._db_lock:
            with self.ctx.conn:
                if tt.db_id is None:
                    cur = self.ctx.conn.execute(
                        "INSERT INTO targets (first_seen, last_seen, latitude, longitude, altitude_m, distance_m, "
                        "error_m, cameras, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", vals)
                    tt.db_id = cur.lastrowid
                else:
                    self.ctx.conn.execute(
                        "UPDATE targets SET first_seen = ?, last_seen = ?, latitude = ?, longitude = ?, altitude_m = ?, "
                        "distance_m = ?, error_m = ?, cameras = ?, status = ? WHERE id = ?", vals + (tt.db_id,))

    # --- для екранів -----------------------------------------------------------------------------------
    def rows(self) -> List[dict]:
        """Таблиця виявлених цілей: один рядок — одна камера (кілька обʼєктів камери — кілька рядків).
        Камера без обʼєктів — рядок лише з назвою. GPS, висота і відстань — лише для обʼєктів, що
        входять у підтверджену ціль."""
        now = self._clock()
        with self._lock:
            member = {}
            for tt in self.targets.values():
                for r in tt.target.rays:
                    member[r.key] = tt
            out = []
            for rec in self.ctx.records():
                t, obs = self._obs.get(rec.name, (0.0, []))
                fresh = [o for o in obs if now - t <= self.FRESH_S]
                if not fresh:
                    out.append({"camera": rec.name})
                    continue
                for o in fresh:
                    row = {"camera": rec.name, "time": o.wall, "az": o.az, "el": o.el, "track": o.track}
                    tt = member.get((rec.name, o.track))
                    if tt is not None:
                        g = tt.geo
                        row.update(target=tt.number, lat=g.latitude, lon=g.longitude, alt=g.altitude_m,
                                   dist=tt.target.distances.get(rec.name), error=tt.target.error_m,
                                   cameras=len(tt.target.rays))
                    out.append(row)
            return out

    def _log(self, camera_id, kind, message) -> None:
        with self.ctx.control._db_lock:
            with self.ctx.conn:
                self.ctx.conn.execute("INSERT INTO events (camera_id, kind, message) VALUES (?, ?, ?)",
                                      (camera_id, kind, message))


def fmt_deg(v: Optional[float], digits: int = 6) -> str:
    return "" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{digits}f}"
