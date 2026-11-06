"""Маска зони виявлення камери (таблиця detection_mask): мертві зони, де рух ігнорується.

Елементи (ТЗ, «Калібрування / Маска зони виявлення»):
* верхня межа — усе вище цього кута місця мертве (kind 'el_max'), одна на камеру;
* нижня межа — усе нижче мертве (kind 'el_min'), одна на камеру;
* прямокутники (kind 'rect') — az_from…az_to (якщо az_from > az_to — через 0°/360°), el_from…el_to.
Координати — у градусах, тож маска не залежить від розміру панорами.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

UPPER, LOWER, RECT = "el_max", "el_min", "rect"


@dataclass
class MaskItem:
    kind: str
    el: Optional[float] = None            # для меж
    az_from: Optional[float] = None       # для прямокутника
    az_to: Optional[float] = None
    el_from: Optional[float] = None
    el_to: Optional[float] = None
    id: Optional[int] = None

    @classmethod
    def rect(cls, az1: float, el1: float, az2: float, el2: float, wrap: bool = False) -> "MaskItem":
        """Прямокутник з двох кутів (як намальовано мишею). wrap=True — через 0°/360°."""
        a, b = sorted((az1 % 360.0, az2 % 360.0))
        if wrap:
            a, b = b, a
        return cls(RECT, az_from=a, az_to=b, el_from=min(el1, el2), el_to=max(el1, el2))

    def contains(self, az, el):
        az = np.asarray(az) % 360.0
        el = np.asarray(el)
        if self.kind == UPPER:
            return el > self.el
        if self.kind == LOWER:
            return el < self.el
        if self.az_from <= self.az_to:
            in_az = (az >= self.az_from) & (az <= self.az_to)
        else:
            in_az = (az >= self.az_from) | (az <= self.az_to)
        return in_az & (el >= self.el_from) & (el <= self.el_to)


class DetectionMask:
    def __init__(self, items: Optional[List[MaskItem]] = None) -> None:
        self.items: List[MaskItem] = list(items or [])

    def set_limit(self, kind: str, el: float) -> None:
        """Верхня або нижня межа — по одній кожного типу."""
        self.items = [i for i in self.items if i.kind != kind]
        self.items.append(MaskItem(kind, el=float(min(90.0, max(0.0, el)))))

    def add_rect(self, item: MaskItem) -> None:
        self.items.append(item)

    def remove(self, item: MaskItem) -> None:
        self.items.remove(item)

    def limit(self, kind: str) -> Optional[MaskItem]:
        return next((i for i in self.items if i.kind == kind), None)

    def rects(self) -> List[MaskItem]:
        return [i for i in self.items if i.kind == RECT]

    def is_dead(self, az, el):
        """True там, де рух ігнорується (працює і з масивами)."""
        dead = np.zeros(np.broadcast(np.asarray(az), np.asarray(el)).shape, bool)
        for i in self.items:
            dead |= i.contains(az, el)
        return dead if dead.ndim else bool(dead)

    def item_at(self, az: float, el: float, tol_deg: float = 1.0) -> Optional[MaskItem]:
        """Елемент під курсором (для «Видалити»): межа в межах tol_deg або прямокутник, що містить точку."""
        for i in reversed(self.items):
            if i.kind in (UPPER, LOWER) and abs(el - i.el) <= tol_deg:
                return i
        for i in reversed(self.rects()):
            if i.contains(az, el):
                return i
        return None


def load_mask(conn: sqlite3.Connection, camera_id: int) -> DetectionMask:
    rows = conn.execute("SELECT id, kind, az_from, az_to, el_from, el_to FROM detection_mask "
                        "WHERE camera_id = ? ORDER BY id", (camera_id,)).fetchall()
    items = []
    for rid, kind, af, at, ef, et in rows:
        if kind == RECT:
            items.append(MaskItem(RECT, az_from=af, az_to=at, el_from=ef, el_to=et, id=rid))
        else:
            items.append(MaskItem(kind, el=ef, id=rid))
    return DetectionMask(items)


def save_mask(conn: sqlite3.Connection, camera_id: int, mask: DetectionMask) -> None:
    """Записати всі елементи маски камери (попередні замінюються)."""
    with conn:
        conn.execute("DELETE FROM detection_mask WHERE camera_id = ?", (camera_id,))
        for i in mask.items:
            if i.kind == RECT:
                conn.execute("INSERT INTO detection_mask (camera_id, kind, az_from, az_to, el_from, el_to) "
                             "VALUES (?, 'rect', ?, ?, ?, ?)", (camera_id, i.az_from, i.az_to, i.el_from, i.el_to))
            else:
                conn.execute("INSERT INTO detection_mask (camera_id, kind, el_from) VALUES (?, ?, ?)",
                             (camera_id, i.kind, i.el))
