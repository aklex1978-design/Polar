"""Моделі камер (меню «Налаштування / Моделі камер»): оптика задається моделлю, камера — посилається на неї.

Модель: назва, кути огляду HFOV/VFOV, дисторсія (k1, k2, p1, p2, k3), довідково — роздільна здатність
сенсора і примітки. Камера зберігає копію оптики своєї моделі (нею користуються проєкція, панорами,
виявлення); зміна моделі одразу оновлює всі її камери, а зміна кутів огляду позначає їхні панорами
застарілими.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

OPTICS = ("fov_x_deg", "fov_y_deg", "k1", "k2", "p1", "p2", "k3")


@dataclass
class CameraModelSpec:
    name: str = ""
    fov_x_deg: float = 60.0
    fov_y_deg: float = 34.0
    k1: float = 0.0
    k2: float = 0.0
    p1: float = 0.0
    p2: float = 0.0
    k3: float = 0.0
    sensor_x: Optional[int] = None
    sensor_y: Optional[int] = None
    notes: str = ""
    id: Optional[int] = None
    cameras: int = 0                    # скільки камер цієї моделі (при читанні)

    def validate(self, others: List["CameraModelSpec"] = ()) -> Dict[str, str]:
        e: Dict[str, str] = {}
        if not self.name.strip():
            e["name"] = "обовʼязкове поле"
        elif any(o.name.strip().lower() == self.name.strip().lower() and o.id != self.id for o in others):
            e["name"] = "модель з такою назвою вже є"
        for f in ("fov_x_deg", "fov_y_deg"):
            if not 0 < getattr(self, f) <= 180:
                e[f] = "0–180°"
        for f in ("k1", "k2", "p1", "p2", "k3"):
            if not -10 <= getattr(self, f) <= 10:
                e[f] = "−10…10"
        if (self.sensor_x is None) != (self.sensor_y is None):
            e["sensor_x"] = "обидва значення або жодного"
        elif self.sensor_x is not None and (self.sensor_x <= 0 or self.sensor_y <= 0):
            e["sensor_x"] = "цілі числа більше 0"
        return e

    @property
    def optics_text(self) -> str:
        return f"{self.fov_x_deg:g}° × {self.fov_y_deg:g}°"

    @property
    def sensor_text(self) -> str:
        return f"{self.sensor_x}×{self.sensor_y}" if self.sensor_x else ""


_COLS = ("name", "fov_x_deg", "fov_y_deg", "k1", "k2", "p1", "p2", "k3", "sensor_x", "sensor_y", "notes")


def list_models(conn: sqlite3.Connection) -> List[CameraModelSpec]:
    rows = conn.execute(f"SELECT m.id, {', '.join('m.' + c for c in _COLS)}, "
                        "(SELECT COUNT(*) FROM cameras c WHERE c.model_id = m.id) "
                        "FROM camera_models m ORDER BY m.name COLLATE NOCASE").fetchall()
    out = []
    for r in rows:
        m = CameraModelSpec(**dict(zip(_COLS, r[1:1 + len(_COLS)])), id=r[0], cameras=r[-1])
        m.notes = m.notes or ""
        out.append(m)
    return out


def get_model(conn: sqlite3.Connection, model_id: int) -> CameraModelSpec:
    m = next((x for x in list_models(conn) if x.id == model_id), None)
    if m is None:
        raise KeyError(model_id)
    return m


def save_model(conn: sqlite3.Connection, m: CameraModelSpec) -> Tuple[int, List[str]]:
    """Зберегти модель і оновити оптику її камер. Повертає (id, назви камер, чиї панорами застаріли)."""
    errors = m.validate(list_models(conn))
    if errors:
        raise ValueError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    m.name = m.name.strip()
    values = [getattr(m, c) for c in _COLS]
    stale: List[str] = []
    with conn:
        if m.id is None:
            cur = conn.execute(f"INSERT INTO camera_models ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
                               values)
            m.id = cur.lastrowid
        else:
            conn.execute(f"UPDATE camera_models SET {', '.join(c + ' = ?' for c in _COLS)} WHERE id = ?",
                         [*values, m.id])
            # кути огляду змінились — панорами камер цієї моделі застаріли
            stale = [r[0] for r in conn.execute(
                "SELECT name FROM cameras WHERE model_id = ? AND (fov_x_deg != ? OR fov_y_deg != ?)",
                (m.id, m.fov_x_deg, m.fov_y_deg))]
            conn.execute("UPDATE cameras SET panorama_stale = 1 WHERE model_id = ? AND (fov_x_deg != ? OR fov_y_deg != ?)",
                         (m.id, m.fov_x_deg, m.fov_y_deg))
            conn.execute(f"UPDATE cameras SET model = ?, {', '.join(c + ' = ?' for c in OPTICS)}, "
                         "updated_at = datetime('now') WHERE model_id = ?",
                         [m.name, *(getattr(m, c) for c in OPTICS), m.id])
    return m.id, stale


def delete_model(conn: sqlite3.Connection, model_id: int) -> None:
    """Видалити модель, якщо жодна камера на неї не посилається."""
    used = [r[0] for r in conn.execute("SELECT name FROM cameras WHERE model_id = ?", (model_id,))]
    if used:
        raise ValueError("модель використовують камери: " + ", ".join(used))
    with conn:
        conn.execute("DELETE FROM camera_models WHERE id = ?", (model_id,))
