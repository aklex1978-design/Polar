"""Виявлення руху: кадр порівнюється з відповідною ділянкою панорами (ТЗ, «Робота / Режим роботи»).

1. Зворотна проєкція: для кожного пікселя кадру — азимут і кут місця → піксель панорами → еталонний кадр.
2. Суміщення ECC (зсув): прибирає похибку датчика положення.
3. Нормалізація яскравості; рух — лише пікселі, ЯСКРАВІШІ за еталон більше ніж на поріг (ціль на
   тепловізорі — яскрава пляма; темніші місця — не ціль).
4. Обʼєкт — суцільна (8-звʼязна, без дір) яскрава пляма будь-якої форми площею від «мінімального розміру»
   пікселів (типово 2, тобто більше 1 пікселя); дрібніші, дірчасті й ті, що в мертвих зонах маски, — відкидаються.
5. Центр кожного обʼєкта → азимут і кут місця за моделлю камери.
6. Ціллю обʼєкт стає, лише коли він є щонайменше на «мінімальній кількості кадрів» поспіль (Tracker):
   одиночні спалахи шуму відкидаються.
Порівняння — лише коли камера стоїть. Кадр обробляється без згладжування і, до ширини 1280, у повній
роздільності — щоб пляма у 2 пікселі не розмилась.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, dataclass, field, fields
from typing import List, Optional, Tuple

import numpy as np

from .mask import DetectionMask
from .panorama import Panorama
from .projection import CameraModel


@dataclass
class DetectionSettings:
    """«Налаштування / Виявлення» (зберігаються в settings з префіксом detection.)."""
    min_area_px: int = 2                 # мінімальний розмір обʼєкта — площа, пікселів кадру (> 1 пікселя)
    min_frames: int = 2                  # обʼєкт має бути щонайменше на стількох кадрах поспіль
    solidity_pct: float = 80.0           # суцільність: частка пікселів плями від її площі без дір, %
    max_range_m: float = 3000.0          # максимальна дальність (етап 6)
    diff_threshold: int = 30             # поріг різниці, 0–255
    reference_update_min: float = 10.0   # частота оновлення еталона, хв; 0 — не оновлювати
    accuracy_deg: float = 0.5            # критерій точності
    hold_s: float = 5.0                  # час утримання режиму «Виявлення»
    k_tolerance: float = 2.0             # коефіцієнт допуску зіставлення (етап 6)
    sync_window_ms: float = 0.0          # 0 — тривалість кадру (етап 6)
    min_ray_angle_deg: float = 5.0       # етап 6
    confirm_frames: int = 3              # етап 6
    # огляд зони в Слідкуванні (polar_core.patrol)
    patrol_dwell_s: float = 3.0          # час огляду кожного положення, с; 0 — камера не рухається
    patrol_el_min_deg: float = 0.0       # смуга огляду за кутом місця (центр кадру), °
    patrol_el_max_deg: float = 30.0
    patrol_step_pct: float = 80.0        # крок між положеннями, % поля зору (100 — без перекриття)
    follow_edge_pct: float = 20.0        # обʼєкт ближче до краю кадру — камера повертається на нього, %
    # рух цілі (polar_core.motion)
    motion_window_s: float = 3.0         # вікно оцінки напрямку і швидкості, с
    predict_s: float = 10.0              # прогноз положення цілі через, с
    # наведення інших камер на підозру (polar_core.cueing)
    cue_others: int = 1                  # 1 — наводити, 0 — ні
    cue_min_range_m: float = 100.0       # пошук уздовж променя — від цієї відстані до максимальної дальності

    def validate(self) -> List[str]:
        e = []
        if not 1 <= self.min_area_px <= 100000:
            e.append("мінімальний розмір обʼєкта — від 1 пікселя")
        if not 1 <= self.min_frames <= 100:
            e.append("мінімальна кількість кадрів — 1–100")
        if not 0 <= self.solidity_pct <= 100:
            e.append("суцільність — 0–100 %")
        if not 0 <= self.diff_threshold <= 255:
            e.append("поріг різниці — 0–255")
        if self.reference_update_min < 0:
            e.append("частота оновлення еталона — 0 або більше")
        if not 0 < self.accuracy_deg <= 10:
            e.append("критерій точності — 0–10°")
        if self.hold_s < 0:
            e.append("час утримання — 0 або більше")
        if self.max_range_m <= 0:
            e.append("максимальна дальність — більше 0")
        if not 0 < self.k_tolerance <= 100:
            e.append("коефіцієнт допуску k — 0–100")
        if self.sync_window_ms < 0:
            e.append("вікно синхронізації — 0 (тривалість кадру) або більше")
        if not 0 <= self.min_ray_angle_deg < 90:
            e.append("мінімальний кут між променями — 0–90°")
        if not 1 <= self.confirm_frames <= 1000:
            e.append("кадрів для підтвердження пари — 1–1000")
        if self.patrol_dwell_s < 0:
            e.append("час огляду положення — 0 або більше")
        if not 0 <= self.patrol_el_min_deg <= self.patrol_el_max_deg <= 90:
            e.append("кут місця огляду — 0 ≤ від ≤ до ≤ 90°")
        if not 10 <= self.patrol_step_pct <= 100:
            e.append("крок огляду — 10–100 % поля зору")
        if not 0 <= self.follow_edge_pct <= 45:
            e.append("край кадру для стеження — 0–45 %")
        if not 0.5 <= self.motion_window_s <= 60:
            e.append("вікно оцінки руху — 0,5–60 с")
        if not 0 <= self.predict_s <= 3600:
            e.append("інтервал прогнозу — 0–3600 с")
        if self.cue_others not in (0, 1):
            e.append("наведення інших камер — 0 або 1")
        if not 1 <= self.cue_min_range_m < self.max_range_m:
            e.append("пошук по променю: мінімальна відстань — від 1 м до максимальної дальності")
        return e


_PREFIX = "detection."     # detection.accuracy_deg — той самий ключ, що читає калібрування


def load_settings(conn: sqlite3.Connection) -> DetectionSettings:
    rows = dict(conn.execute("SELECT key, value FROM settings WHERE key LIKE 'detection.%'").fetchall())
    s = DetectionSettings()
    for f in fields(DetectionSettings):
        raw = rows.get(_PREFIX + f.name)
        if raw is not None:
            try:
                cur = getattr(s, f.name)
                setattr(s, f.name, int(float(raw)) if isinstance(cur, int) else float(raw))
            except ValueError:
                pass
    return s


def save_settings(conn: sqlite3.Connection, s: DetectionSettings) -> None:
    errors = s.validate()
    if errors:
        raise ValueError("; ".join(errors))
    with conn:
        for k, v in asdict(s).items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_PREFIX + k, str(v)))


@dataclass
class DetectedObject:
    u: float                 # центр у пікселях кадру (повного розміру)
    v: float
    w: float
    h: float
    area_px: float
    az: float                # азимут від півночі
    el: float
    size_deg: float


@dataclass
class DetectionResult:
    objects: List[DetectedObject]
    aligned_shift_px: Tuple[float, float] = (0.0, 0.0)
    compared: bool = True
    reason: str = ""
    motion_px: int = 0                  # пікселів руху до фільтрації за розміром і маскою
    candidates: List[DetectedObject] = field(default_factory=list)   # усі плями кадру, і ще не підтверджені


def gray(img: np.ndarray) -> np.ndarray:
    import cv2

    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


class Detector:
    """Детектор однієї камери. Працює на зменшеному кадрі (ширина ≤ work_width)."""
    PAD = 4

    def __init__(self, settings: DetectionSettings, work_width: int = 1280, edge_tolerance_px: int = 2) -> None:
        self.settings = settings
        self.work_width = work_width
        self.edge_tolerance_px = edge_tolerance_px
        self._pano_gray: Optional[np.ndarray] = None
        self._pano_valid: Optional[np.ndarray] = None
        self._pano_key = None
        self._grid_key = None
        self._grid = None

    def _panorama_layers(self, pano: Panorama, key) -> None:
        if key != self._pano_key or self._pano_gray is None:
            # по горизонталі панорама замкнена (0° = 360°): дописуємо кілька стовпців з протилежного краю;
            # по вертикалі — ні (90° і 0° — різні місця), тож там BORDER_REPLICATE, а не WRAP
            pad = self.PAD
            g = gray(pano.image).astype(np.float32)
            c = pano.covered().astype(np.float32)
            self._pano_gray = np.hstack([g[:, -pad:], g, g[:, :pad]])
            self._pano_valid = np.hstack([c[:, -pad:], c, c[:, :pad]])
            self._pano_key = key

    def reference(self, pano: Panorama, model: CameraModel, sensor_az: float, el: float,
                  pano_key=None) -> Tuple[np.ndarray, np.ndarray]:
        """Еталон — вигляд панорами з цього положення камери (зворотна проєкція), і де він є."""
        import cv2

        self._panorama_layers(pano, pano_key if pano_key is not None else id(pano))
        key = (model.width, model.height, model.cx, model.cy, model.k1, model.k2, model.p1, model.p2, model.k3,
               model.tilt_deg, model.north_offset_deg, round(sensor_az, 3), round(el, 3), pano.k)
        if key != self._grid_key:
            u, v = np.meshgrid(np.arange(model.width) + 0.5, np.arange(model.height) + 0.5)
            az, elv = model.pixel_to_angles(u, v, sensor_az, el)
            mx = (np.asarray(az) % 360.0 * pano.k - 0.5 + self.PAD).astype(np.float32)
            my = ((90.0 - np.asarray(elv)) * pano.k - 0.5).astype(np.float32)
            self._grid = (mx, my, np.asarray(elv))
            self._grid_key = key
        mx, my, elv = self._grid
        ref = cv2.remap(self._pano_gray, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        valid = cv2.remap(self._pano_valid, mx, my, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT)
        valid = (valid > 0.5) & (elv >= 0) & (elv <= 90)       # під горизонтом панорами немає
        return ref, valid

    def detect(self, frame: np.ndarray, pano: Panorama, model_full: CameraModel, sensor_az: float, el: float,
               mask: Optional[DetectionMask] = None, moving: bool = False, pano_key=None) -> DetectionResult:
        import cv2

        if moving:
            return DetectionResult([], compared=False, reason="камера рухається")
        h0, w0 = frame.shape[:2]
        scale = min(1.0, self.work_width / w0)
        w, h = int(round(w0 * scale)), int(round(h0 * scale))
        g = gray(frame)
        if scale < 1.0:
            g = cv2.resize(g, (w, h), interpolation=cv2.INTER_AREA)
        model = CameraModel(w, h, model_full.hfov_deg, model_full.vfov_deg, model_full.cx * w / w0,
                            model_full.cy * h / h0, model_full.k1, model_full.k2, model_full.p1, model_full.p2,
                            model_full.k3, model_full.tilt_deg, model_full.north_offset_deg)
        ref, valid = self.reference(pano, model, sensor_az, el, pano_key)
        if valid.mean() < 0.05:
            return DetectionResult([], compared=False, reason="у панорамі немає цієї ділянки")
        cur = g.astype(np.float32)
        # суміщення ECC: зсув кадру відносно еталона (похибка датчика)
        shift = (0.0, 0.0)
        warp = np.eye(2, 3, dtype=np.float32)
        try:
            vm = cv2.erode(valid.astype(np.uint8), np.ones((5, 5), np.uint8))
            _cc, warp = cv2.findTransformECC(cv2.GaussianBlur(ref, (5, 5), 0), cv2.GaussianBlur(cur, (5, 5), 0),
                                             warp, cv2.MOTION_TRANSLATION,
                                             (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4), vm, 5)
            if abs(warp[0, 2]) > w * 0.1 or abs(warp[1, 2]) > h * 0.1:
                warp = np.eye(2, 3, dtype=np.float32)            # неправдоподібне суміщення — без нього
            shift = (float(warp[0, 2]), float(warp[1, 2]))
        except cv2.error:
            pass
        # кадр не перетягується (інтерполяція розмила б пляму в 2 пікселі) — зсувається гладкий еталон:
        # точка x еталона лягає на x + зсув кадру
        ref = cv2.warpAffine(ref, warp, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        avalid = cv2.warpAffine(valid.astype(np.uint8), warp, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        avalid = cv2.erode((avalid > 0).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        aligned = cur
        # нормалізація яскравості проти змін освітлення
        if avalid.sum() > 100:
            mr, sr = ref[avalid].mean(), ref[avalid].std() + 1e-3
            mc, sc = aligned[avalid].mean(), aligned[avalid].std() + 1e-3
            aligned = (aligned - mc) * (sr / sc) + mr
        # яскравіше за еталон з допуском у сусідстві: піксель — рух, лише якщо він яскравіший за НАЙЯСКРАВІШИЙ
        # піксель еталона в околі ±tol px. Так різкі краї (дахи, дроти) не дають хибних спрацювань через
        # субпіксельне зміщення між кадром і панорамою. Кадр не згладжується — пляма в 2 пікселі зберігається;
        # еталон (панорама) і так гладкий.
        r = cv2.GaussianBlur(ref, (3, 3), 0)
        k = np.ones((2 * self.edge_tolerance_px + 1,) * 2, np.uint8)
        diff = aligned - cv2.dilate(r, k)
        motion = ((diff > self.settings.diff_threshold) & avalid).astype(np.uint8)
        n, labels, stats, cents = cv2.connectedComponentsWithStats(motion, 8)
        px_per_deg = model.f * math.pi / 180.0
        min_area = max(1.0, self.settings.min_area_px * scale * scale)      # площа в пікселях повного кадру
        min_solid = self.settings.solidity_pct / 100.0
        objects = []
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            if area < min_area:
                continue
            if min_solid > 0 and area >= 8 and area / _filled_area(labels[y:y + bh, x:x + bw] == i) < min_solid:
                continue                                     # не суцільна: пляма з дірами (кільце, сітка)
            if not _stands_out(g, labels, i, x, y, bw, bh, self.settings.diff_threshold / 2.0):
                continue                                     # не пляма: на кадрі не яскравіша за своє оточення
            uf, vf = float(cents[i][0]), float(cents[i][1])
            # кути — з координат еталона (точка кадру мінус зсув): похибку датчика прибрано
            az, elv = model.pixel_to_angles(uf - shift[0], vf - shift[1], sensor_az, el)
            if mask is not None and mask.is_dead(az, elv):
                continue
            objects.append(DetectedObject(uf / scale, vf / scale, bw / scale, bh / scale, float(area) / scale ** 2,
                                          float(az), float(elv), float(max(bw, bh) / px_per_deg)))
        return DetectionResult(objects, shift, True, "", int(motion.sum()))


def _stands_out(img: np.ndarray, labels: np.ndarray, i: int, x: int, y: int, bw: int, bh: int,
                margin: float, ring_px: int = 2) -> bool:
    """Пляма яскравіша за своє оточення на самому кадрі (кільце ring_px навколо неї, 75-й перцентиль).
    Так відкидаються «смужки» неба на краях будинків і хмар через неточне суміщення з панорамою:
    вони яскравіші за еталон, але не за сусідні пікселі кадру."""
    import cv2

    h, w = labels.shape
    x0, y0 = max(0, x - ring_px), max(0, y - ring_px)
    x1, y1 = min(w, x + bw + ring_px), min(h, y + bh + ring_px)
    blob = labels[y0:y1, x0:x1] == i
    ring = cv2.dilate(blob.astype(np.uint8), np.ones((2 * ring_px + 1,) * 2, np.uint8)).astype(bool) & ~blob
    if not ring.any():
        return True
    patch = img[y0:y1, x0:x1].astype(np.float32)
    return float(patch[blob].mean()) - float(np.percentile(patch[ring], 75)) > margin


def _filled_area(blob: np.ndarray) -> float:
    """Площа плями разом із дірами всередині (зовнішній контур, залитий)."""
    import cv2

    pad = np.pad(blob.astype(np.uint8), 1)
    contours, _h = cv2.findContours(pad, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    filled = np.zeros_like(pad)
    cv2.drawContours(filled, contours, -1, 1, thickness=cv2.FILLED)
    return float(max(1, int(filled.sum())))


@dataclass
class Track:
    id: int
    az: float
    el: float
    first_t: float
    last_t: float
    hits: int = 1
    last_saved_t: float = -1e9
    streak: int = 1             # кадрів поспіль з обʼєктом
    confirmed: bool = False     # набрав мінімальну кількість кадрів поспіль — ціль


class Tracker:
    """Супровід обʼєктів між кадрами: найближчий трек у межах gate_deg продовжується, решта — нові треки;
    трек зникає, якщо не бачили довше lost_s. Обʼєкт стає ціллю (confirmed), коли він є на min_frames
    кадрах поспіль; до того його не видно ні в подіях, ні в режимах — одиночні спалахи шуму відкидаються."""

    def __init__(self, gate_deg: float = 3.0, lost_s: float = 2.0, min_frames: int = 1) -> None:
        self.gate_deg = gate_deg
        self.lost_s = lost_s
        self.min_frames = max(1, min_frames)
        self.tracks: List[Track] = []
        self._next = 1

    def update(self, objects: List[DetectedObject], t: float) -> Tuple[List[Tuple[Track, DetectedObject]], List[Track]]:
        """Кадр, порівняний з панорамою. Повертає (пари підтверджений трек–обʼєкт цього кадру,
        треки, підтверджені саме на цьому кадрі)."""
        from .angles import angular_distance

        pairs, new = [], []
        free = list(self.tracks)
        for o in objects:
            best = min(free, key=lambda tr: angular_distance(tr.az, tr.el, o.az, o.el), default=None)
            if best is not None and angular_distance(best.az, best.el, o.az, o.el) <= self.gate_deg:
                free.remove(best)
                best.az, best.el, best.last_t = o.az, o.el, t
                best.hits += 1
                best.streak += 1
                tr = best
            else:
                tr = Track(self._next, o.az, o.el, t, t)
                self._next += 1
                self.tracks.append(tr)
            if not tr.confirmed and tr.streak >= self.min_frames:
                tr.confirmed = True
                new.append(tr)
            if tr.confirmed:
                pairs.append((tr, o))
        for tr in free:                                      # на цьому кадрі обʼєкта немає — серія переривається
            tr.streak = 0
        self.tracks = [tr for tr in self.tracks if t - tr.last_t <= self.lost_s]
        return pairs, new


# --- режими ----------------------------------------------------------------------------------
LEARNING, TRACKING, DETECTION, IDLE = "learning", "tracking", "detection", "idle"
MODE_TEXT = {LEARNING: "Навчання", TRACKING: "Слідкування", DETECTION: "Виявлення", IDLE: "Очікування"}


class ModeMachine:
    """Слідкування ↔ Виявлення: рух на будь-якій камері → Виявлення; руху немає на всіх камерах
    довше за час утримання → назад у Слідкування."""

    def __init__(self, hold_s: float) -> None:
        self.hold_s = hold_s
        self.mode = IDLE
        self._last_motion: Optional[float] = None

    def start_tracking(self) -> None:
        self.mode = TRACKING
        self._last_motion = None

    def stop(self) -> None:
        self.mode = IDLE

    def update(self, motion: bool, t: float) -> str:
        if self.mode not in (TRACKING, DETECTION):
            return self.mode
        if motion:
            self._last_motion = t
            self.mode = DETECTION
        elif self.mode == DETECTION and self._last_motion is not None and t - self._last_motion >= self.hold_s:
            self.mode = TRACKING
        return self.mode
