"""Конфігурація користувача (поза базою): який файл бази SQLite відкривати.

Файл бази — єдине, що не може зберігатись у самій базі. Його шлях лежить у config.json у теці
користувача: Windows — %APPDATA%\\Polar, інші системи — ~/.config/polar. Так оператор запускає
програму без командного рядка, а «Налаштування / Загальні» можуть змінити базу.
"""
from __future__ import annotations

import json
import os
from typing import Optional

DEFAULT_DB = "polar.sqlite"


def config_dir() -> str:
    override = os.environ.get("POLAR_CONFIG_DIR")
    if override:
        return override
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Polar")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "polar")


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def load() -> dict:
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    os.makedirs(config_dir(), exist_ok=True)
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, config_path())


def db_path(cli: Optional[str] = None) -> str:
    """Файл бази: з командного рядка, інакше — запамʼятований, інакше — polar.sqlite у поточній теці."""
    if cli:
        return os.path.abspath(cli)
    remembered = load().get("db_path")
    if remembered:
        return remembered
    return os.path.abspath(DEFAULT_DB)


def remember_db(path: str) -> None:
    data = load()
    data["db_path"] = os.path.abspath(path)
    save(data)
