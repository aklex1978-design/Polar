"""Огляд зони в режимі Слідкування і стеження за підозрілою ціллю.

Камера послідовно обходить положення в дозволених межах (азимут — межі сервопривода, кут місця — смуга огляду
з налаштувань), на кожному стоїть «час огляду» і порівнює кадри з панорамою. Рух → камера зупиняється
(підозра на ціль) і аналізує його; коли обʼєкт підходить до краю кадру — камера повертається на нього, щоб
ціль лишалась у кадрі. Руху немає довше за час утримання — огляд продовжується з того самого положення.

Підозра (азимут і кут місця від півночі) — те, що надалі отримуватимуть інші камери, щоб навестись на ціль
і визначити її координати.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .angles import wrap180, wrap360
from .learning import limit_plan
from .motion import AngularMotion, AngularTrack

PATROL = "огляд"
ALERT = "підозра на ціль"
CUE = "наведення"

ARRIVE_TOL_DEG = 1.5        # камера дійшла до цілі
STOPPED_AFTER_S = 1.5       # або вже стоїть (відійшла і зупинилась) — напр. сервопривід не добирає частку градуса
MOVE_TIMEOUT_S = 20.0       # не дійшла (межа, заклинило) — далі однаково
LEAD_S = 0.5                # стеження: ціль, що рухається, — навестись туди, де вона буде за стільки


def patrol_plan(hfov_deg: float, vfov_deg: float, el_min: float, el_max: float, step_frac: float,
                north_offset_deg: float = 0.0, pan_limits: Optional[Tuple[float, float]] = None
                ) -> List[Tuple[float, float]]:
    """Положення огляду (азимут від півночі, кут місця): ряди змійкою, крок — частка поля зору
    (0,8 — сусідні кадри перекриваються на 20 %). pan_limits — межі кута датчика азимуту (мін < макс)."""
    step_frac = min(1.0, max(0.1, step_frac))
    n_az = max(1, int(math.ceil(360.0 / (hfov_deg * step_frac) - 1e-9)))
    rows = []
    e = max(0.0, el_min)
    while True:                       # верхній ряд — коли верх кадру дістає до el_max
        rows.append(min(e, el_max))
        if e + vfov_deg / 2.0 >= el_max or e >= el_max:
            break
        e += vfov_deg * step_frac
    plan = []
    for i, el in enumerate(rows):
        azs = [k * 360.0 / n_az for k in range(n_az)]
        plan.extend((az, el) for az in (reversed(azs) if i % 2 else azs))
    if pan_limits is not None:
        plan = limit_plan(plan, pan_limits[0], pan_limits[1])
    return [(wrap360(az + north_offset_deg), el) for az, el in plan]


def off_center(u: float, v: float, width: int, height: int, center_frac: float) -> bool:
    """Центр обʼєкта далі від середини кадру, ніж center_frac його ширини (висоти)."""
    return abs(u - width / 2.0) > width * center_frac or abs(v - height / 2.0) > height * center_frac


@dataclass
class Suspect:
    az: float           # від півночі
    el: float
    since: float
    last_seen: float
    follows: int = 0    # скільки разів камера поверталась за ціллю
    track: AngularTrack = field(default_factory=AngularTrack)

    def seen(self, t: float, az: float, el: float) -> None:
        """Кути цілі з поточного кадру (за моделлю калібрування камери)."""
        self.az, self.el, self.last_seen = az, el, t
        self.track.add(t, az, el)

    @property
    def motion(self) -> Optional[AngularMotion]:
        return self.track.motion()


@dataclass
class Cue:
    """Наведення від інших камер: kind «point» — на ціль з координатами (один вузол, оновлюється),
    «search» — пошук уздовж променя камери, що бачить підозру (вузли від ближніх до дальніх)."""
    kind: str
    nodes: List[Tuple[float, float]]
    label: str
    key: tuple
    tol_deg: float = 3.0            # «point»: на скільки має зсунутись ціль, щоб камера перенавелась


class Patroller:
    """Стан огляду однієї камери. update() викликається на кожен кадр і повертає нову ціль камери
    (азимут від півночі, кут місця) або None; ціль після обмеження межами передається в commanded()."""

    def __init__(self, plan: Sequence[Tuple[float, float]], dwell_s: float, hold_s: float, center_frac: float,
                 cyclic: bool = True, window_s: float = 3.0, predict_s: float = 10.0) -> None:
        self.window_s = window_s        # вікно оцінки руху підозри
        self.predict_s = predict_s      # прогноз кутів через стільки секунд
        self.plan = list(plan)
        self.dwell_s = dwell_s
        self.hold_s = hold_s
        self.center_frac = center_frac  # стеження: обʼєкт далі від середини кадру — камера наводиться на нього
        self.cyclic = cyclic            # повне коло — по колу; у межах азимуту — туди й назад
        self.state = PATROL
        self.index = 0
        self._dir = 1
        self.target: Optional[Tuple[float, float]] = None
        self._cmd_t = 0.0
        self._arrived_t: Optional[float] = None
        self.suspect: Optional[Suspect] = None
        self.cue_target: Optional[Cue] = None
        self.cue_index = 0
        self._cue_dir = 1
        self._cue_req = None            # з потоку наведення: ("set", Cue | None); застосовується в update()

    # --- допоміжне ---------------------------------------------------------------------
    def _issue(self, target: Tuple[float, float], t: float) -> Tuple[float, float]:
        self.target = target
        self._cmd_t = t
        self._arrived_t = None
        return target

    def commanded(self, target: Tuple[float, float]) -> None:
        """Фактична ціль після обмеження межами камери."""
        self.target = target

    def _arrived(self, t: float, pose: Optional[Tuple[float, float]], still: bool) -> bool:
        if self.target is None:
            return True
        if t - self._cmd_t > MOVE_TIMEOUT_S:
            return still
        if pose is None or not still:
            return False
        if t - self._cmd_t >= STOPPED_AFTER_S:
            return True
        return (abs(wrap180(pose[0] - self.target[0])) <= ARRIVE_TOL_DEG
                and abs(pose[1] - self.target[1]) <= ARRIVE_TOL_DEG)

    def _advance(self) -> None:
        n = len(self.plan)
        if n <= 1:
            return
        if self.cyclic:
            self.index = (self.index + 1) % n
            return
        if not 0 <= self.index + self._dir < n:
            self._dir = -self._dir
        self.index += self._dir

    def cue(self, cue: Optional[Cue]) -> None:
        """Запит наведення (з іншого потоку); None — наведення не потрібне, далі огляд."""
        self._cue_req = ("set", cue)

    def _apply_cue(self, cue: Optional[Cue], t: float) -> Optional[Tuple[float, float]]:
        if self.state == ALERT:                     # своя підозра важливіша — запит ігнорується
            return None
        if cue is None or not cue.nodes:
            if self.state == CUE:
                self.state, self.cue_target = PATROL, None
                return self._issue(self.plan[self.index], t)
            return None
        cur = self.cue_target
        if self.state == CUE and cur is not None and cur.key == cue.key:
            if cue.kind == "point":
                new = cue.nodes[0]
                moved = max(abs(wrap180(new[0] - cur.nodes[0][0])), abs(new[1] - cur.nodes[0][1]))
                if moved > cue.tol_deg:
                    self.cue_target = cue
                    return self._issue(new, t)
                cur.label = cue.label
                return None
            self.cue_target = cue                   # промінь трохи змістився — нові вузли, той самий хід
            self.cue_index = min(self.cue_index, len(cue.nodes) - 1)
            return None
        self.state, self.cue_target, self.cue_index, self._cue_dir = CUE, cue, 0, 1
        return self._issue(cue.nodes[0], t)

    def _advance_cue(self) -> None:
        n = len(self.cue_target.nodes)
        if n <= 1:
            return
        if not 0 <= self.cue_index + self._cue_dir < n:
            self._cue_dir = -self._cue_dir
        self.cue_index += self._cue_dir

    def _start_alert(self, o, t: float, frame_size) -> Optional[Tuple[float, float]]:
        self.state = ALERT
        self.suspect = Suspect(o.az, o.el, t, t, track=AngularTrack(self.window_s))
        self.suspect.seen(t, o.az, o.el)
        return self._follow(o, t, frame_size)

    @staticmethod
    def _primary(objects):
        return max(objects, key=lambda o: o.area_px)

    # --- крок ---------------------------------------------------------------------------
    def update(self, t: float, pose: Optional[Tuple[float, float]], still: bool, compared: bool,
               objects: Sequence = (), frame_size: Tuple[int, int] = (0, 0)) -> Optional[Tuple[float, float]]:
        """pose — поточні (азимут від півночі, кут місця) камери; still — камера стоїть і кадри вже не змазані;
        compared — кадр порівняно з панорамою; objects — виявлені обʼєкти (DetectedObject)."""
        if not self.plan:
            return None
        req, self._cue_req = self._cue_req, None
        if req is not None:
            cmd = self._apply_cue(req[1], t)
            if cmd is not None:
                return cmd
        arrived = self._arrived(t, pose, still)
        if arrived and self._arrived_t is None:
            self._arrived_t = t
        seen = list(objects) if (compared and arrived) else []

        if self.state == PATROL:
            if self.target is None:
                return self._issue(self.plan[self.index], t)
            if not arrived:
                return None
            if seen:                                            # рух — зупинитись і аналізувати
                return self._start_alert(self._primary(seen), t, frame_size)
            if t - self._arrived_t >= self.dwell_s:
                self._advance()
                return self._issue(self.plan[self.index], t)
            return None

        if self.state == CUE:                                   # навелась за іншими камерами
            if not arrived:
                return None
            if seen:
                self.cue_target = None
                return self._start_alert(self._primary(seen), t, frame_size)
            if self.cue_target.kind == "search" and t - self._arrived_t >= self.dwell_s:
                self._advance_cue()
                return self._issue(self.cue_target.nodes[self.cue_index], t)
            return None

        # ALERT: камера стоїть на цілі; за краєм кадру — повертається за нею
        if seen:
            o = self._primary(seen)
            self.suspect.seen(t, o.az, o.el)
            return self._follow(o, t, frame_size)
        last = self.suspect.last_seen if self.suspect else t
        if arrived and t - max(last, self._arrived_t or t) >= self.hold_s:
            self.state = PATROL                                  # ціль зникла — огляд далі з того ж положення
            self.suspect = None
            return self._issue(self.plan[self.index], t)
        return None

    def _follow(self, o, t: float, frame_size: Tuple[int, int]) -> Optional[Tuple[float, float]]:
        """Тримати ціль у середині кадру: зійшла з середини — навестись на неї (з упередженням на рух)."""
        w, h = frame_size
        if not (w and h and off_center(o.u, o.v, w, h, self.center_frac)):
            return None
        self.suspect.follows += 1
        m = self.suspect.motion
        az, el = m.predict(LEAD_S) if m is not None else (o.az, o.el)
        return self._issue((wrap360(az), el), t)

    # --- для екранів ------------------------------------------------------------------------
    def text(self) -> str:
        if self.state == ALERT and self.suspect is not None:
            s = self.suspect
            az = round(s.az % 360.0, 1) % 360.0
            text = f"ПІДОЗРА НА ЦІЛЬ: азимут {az:.1f}°, кут місця {s.el:.1f}°"
            m = s.motion
            if m is not None:
                paz, pel = m.predict(self.predict_s)
                text += (f" · рух {m.direction_text()} ({m.az_rate:+.2f}°/с, {m.el_rate:+.2f}°/с)"
                         f" · через {self.predict_s:g} с: {round(paz, 1) % 360.0:.1f}° / {pel:.1f}°")
            return text + (f" · камера йде за ціллю ({s.follows})" if s.follows else "")
        if self.state == CUE and self.cue_target is not None:
            c = self.cue_target
            if c.kind == "point":
                return f"НАВЕДЕННЯ на {c.label}"
            return f"НАВЕДЕННЯ: пошук по {c.label} · положення {self.cue_index + 1} з {len(c.nodes)}"
        return f"огляд: положення {self.index + 1} з {len(self.plan)}"

