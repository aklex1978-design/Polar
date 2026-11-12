"""Журнал подій (таблиця events): назви подій, вибірка з фільтрами, експорт CSV."""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional

# тип події → назва для оператора; невідомі типи показуються як є
EVENT_KINDS = {
    "mode": "Режим",
    "motion": "Рух",
    "target": "Ціль",
    "target_lost": "Ціль зникла",
    "targets_error": "Помилка розрахунку цілей",
    "learning_done": "Навчання завершено",
    "learning_stopped": "Навчання зупинено",
    "learning_error": "Помилка Навчання",
    "manual_save": "Ручне збереження кадру",
    "map_loaded": "Карту завантажено",
    "stream_ok": "Потік працює",
    "stream_lost": "Потік втрачено",
    "settings": "Налаштування",
    "error": "Помилка програми",
    "warning": "Попередження",
    "start": "Запуск програми",
}
# події, про які варто повідомити в рядку стану
ALERT_KINDS = ("error", "learning_error", "targets_error", "stream_lost", "warning")
PERIODS = {"Остання година": 3600, "Остання доба": 86400, "Останній тиждень": 7 * 86400, "Усі": None}


@dataclass
class Event:
    id: int
    ts: dt.datetime                 # UTC
    camera: str
    kind: str
    message: str

    @property
    def kind_text(self) -> str:
        return EVENT_KINDS.get(self.kind, self.kind)

    @property
    def local_time(self) -> str:
        return self.ts.astimezone().strftime("%d.%m.%Y %H:%M:%S")


def _parse_ts(raw: str) -> dt.datetime:
    try:
        t = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.now(dt.timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def query(conn, camera_id: Optional[int] = None, kinds: Optional[List[str]] = None,
          since_s: Optional[float] = None, text: str = "", limit: int = 2000,
          after_id: int = 0, no_camera: bool = False) -> List[Event]:
    """Події від нових до старих. camera_id=None — усі камери; no_camera — лише загальні (без камери)."""
    sql = ["SELECT e.id, e.ts, COALESCE(c.name, ''), e.kind, e.message FROM events e "
           "LEFT JOIN cameras c ON c.id = e.camera_id WHERE e.id > ?"]
    args: list = [after_id]
    if no_camera:
        sql.append("AND e.camera_id IS NULL")
    elif camera_id is not None:
        sql.append("AND e.camera_id = ?")
        args.append(camera_id)
    if kinds:
        sql.append(f"AND e.kind IN ({','.join('?' * len(kinds))})")
        args.extend(kinds)
    if since_s is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=since_s)
        sql.append("AND e.ts >= ?")
        args.append(cutoff.strftime("%Y-%m-%d %H:%M:%S"))
    if text:
        sql.append("AND e.message LIKE ?")
        args.append(f"%{text}%")
    sql.append("ORDER BY e.id DESC LIMIT ?")
    args.append(limit)
    rows = conn.execute(" ".join(sql), args).fetchall()
    return [Event(r[0], _parse_ts(r[1]), r[2], r[3], r[4]) for r in rows]


def kinds_present(conn) -> List[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT kind FROM events ORDER BY kind").fetchall()]


def export_csv(events: List[Event], path: str) -> None:
    """CSV для Excel: UTF-8 з BOM, крапка з комою."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Час", "Камера", "Подія", "Повідомлення"])
        for e in events:
            w.writerow([e.local_time, e.camera, e.kind_text, e.message])
