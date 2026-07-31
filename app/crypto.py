from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "ACCOUNT_ENCRYPTION_KEY must be a valid Fernet key"
            ) from exc

    def encrypt(self, value: str) -> str:
        if not value:
            raise ValueError("Cannot encrypt an empty secret")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Stored credential cannot be decrypted") from exc

    @staticmethod
    def fingerprint(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
