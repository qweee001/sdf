from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    """Fernet 加密/解密（帳號 session 用），附 sha256 指紋"""

    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "ACCOUNT_ENCRYPTION_KEY 必須是合法的 Fernet 金鑰"
            ) from exc

    def encrypt(self, value: str) -> str:
        if not value:
            raise ValueError("不能加密空字串")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("儲存的憑證無法解密") from exc

    @staticmethod
    def fingerprint(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
