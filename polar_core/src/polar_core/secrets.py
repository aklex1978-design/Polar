"""Зашифроване зберігання паролів відеопотоку.

Ключ — окремий файл поруч із базою (<база>.key, доступ лише власнику). Без ключа зашифровані
паролі з бази прочитати не можна, тож копія бази без ключа паролів не розкриває.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Union

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._f = Fernet(key)

    @classmethod
    def for_database(cls, db_path: Union[str, Path]) -> "SecretBox":
        if str(db_path) == ":memory:":
            return cls(Fernet.generate_key())
        key_path = Path(str(db_path) + ".key")
        if key_path.exists():
            return cls(key_path.read_bytes().strip())
        key = Fernet.generate_key()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return cls(key)

    def encrypt(self, plain: str) -> str:
        return self._f.encrypt(plain.encode()).decode() if plain else ""

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise ValueError("пароль зашифровано іншим ключем — введіть його заново") from e
