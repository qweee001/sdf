"""
Telegram 登入流程 - 電話 + 驗證碼 +（可選）2FA 密碼
控制台新增水軍帳號時，水軍帳號需要重新登入取得 session。
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.utils import get_display_name


class LoginExpired(ValueError):
    pass


class LoginConflict(RuntimeError):
    pass


class LoginRateLimit(RuntimeError):
    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = max(int(retry_after), 1)


FLOW_TTL = 10 * 60
AUTHORIZED_TTL = 5 * 60
START_COOLDOWN = 60
MAX_CODE_ATTEMPTS = 5
MAX_PASSWORD_ATTEMPTS = 3


@dataclass
class VerifiedSession:
    session_string: str
    tg_user_id: int
    tg_name: str


@dataclass
class PendingLogin:
    auth_id: str
    phone: str
    phone_hint: str
    phone_code_hash: str
    client: TelegramClient
    created_at: float
    expires_at: float
    state: str = "code_required"
    code_attempts: int = 0
    password_attempts: int = 0
    verified: VerifiedSession | None = None
    claimed: bool = False


class TelegramLoginService:
    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.pending: dict[str, PendingLogin] = {}
        self._last_start: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _clean_phone(value: object) -> str:
        phone = re.sub(r"[\s()\-]", "", str(value or ""))
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
            raise ValueError("請輸入含國碼的手機號碼，例如 +886912345678")
        return phone

    @staticmethod
    def _mask_phone(phone: str) -> str:
        return f"{phone[:4]}•••••{phone[-3:]}" if len(phone) > 7 else phone

    @staticmethod
    def _clean_code(value: object) -> str:
        code = re.sub(r"\s", "", str(value or ""))
        if not re.fullmatch(r"[0-9]{4,8}", code):
            raise ValueError("請輸入 Telegram 傳送的數字驗證碼")
        return code

    def _public(self, p: PendingLogin) -> dict:
        return {
            "auth_id": p.auth_id,
            "status": p.state,
            "phone_hint": p.phone_hint,
            "expires_in": max(int(p.expires_at - time.time()), 0),
        }

    async def _disconnect(self, client: TelegramClient):
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass

    async def _remove(self, auth_id: str) -> None:
        p = self.pending.pop(auth_id, None)
        if p:
            await self._disconnect(p.client)

    async def start(self, phone_value: object) -> dict:
        phone = self._clean_phone(phone_value)
        now = time.time()
        async with self._lock:
            last = self._last_start.get(phone, 0)
            if now - last < START_COOLDOWN:
                raise LoginRateLimit(
                    f"驗證碼請求太頻繁，請 {int(START_COOLDOWN - (now - last)) + 1} 秒後再試",
                    int(START_COOLDOWN - (now - last)) + 1,
                )
            self._last_start[phone] = now

        client = TelegramClient(StringSession(), self.api_id, self.api_hash)
        try:
            try:
                await asyncio.wait_for(client.connect(), timeout=30)
                sent = await asyncio.wait_for(
                    client.send_code_request(phone), timeout=30
                )
            except asyncio.TimeoutError:
                raise LoginRateLimit("Telegram 連線逾時，請稍後再試") from None
            except FloodWaitError as e:
                raise LoginRateLimit(
                    "Telegram 要求稍後再傳送驗證碼", int(e.seconds)
                ) from None
            except PhoneNumberFloodError:
                raise LoginRateLimit(
                    "這個號碼的驗證要求過多，請稍後再試", START_COOLDOWN
                ) from None
            except PhoneNumberInvalidError:
                raise ValueError("手機號碼格式無效，請確認國碼與號碼") from None
            except PhoneNumberBannedError:
                raise ValueError("這個 Telegram 手機號碼目前無法登入") from None
            except Exception:
                raise LoginRateLimit(
                    "Telegram 暫時無法傳送驗證碼，請稍後再試"
                ) from None
        except Exception:
            await self._disconnect(client)
            raise

        auth_id = secrets.token_urlsafe(32)
        p = PendingLogin(
            auth_id=auth_id,
            phone=phone,
            phone_hint=self._mask_phone(phone),
            phone_code_hash=str(sent.phone_code_hash),
            client=client,
            created_at=now,
            expires_at=now + FLOW_TTL,
        )
        self.pending[auth_id] = p
        return self._public(p)

    async def _get(self, auth_id: object) -> PendingLogin:
        aid = str(auth_id or "").strip()
        p = self.pending.get(aid)
        if p is None:
            raise LoginExpired("登入流程不存在或已過期")
        if time.time() > p.expires_at:
            await self._remove(aid)
            raise LoginExpired("登入流程已過期，請重新取得驗證碼")
        return p

    async def _authorize(self, p: PendingLogin) -> None:
        me = await p.client.get_me()
        if me is None:
            raise LoginExpired("無法取得 Telegram 帳號資料")
        session_string = p.client.session.save()
        if not session_string:
            raise LoginExpired("Telegram Session 建立失敗")
        p.verified = VerifiedSession(
            session_string=session_string,
            tg_user_id=int(me.id),
            tg_name=get_display_name(me) or str(me.id),
        )
        p.state = "authorized"
        p.phone = ""
        p.expires_at = min(p.created_at + FLOW_TTL, time.time() + AUTHORIZED_TTL)
        await self._disconnect(p.client)

    async def submit_code(self, auth_id: object, code_value: object) -> dict:
        p = await self._get(auth_id)
        code = self._clean_code(code_value)
        if p.state != "code_required":
            raise LoginConflict("目前登入流程不接受驗證碼")
        try:
            await p.client.sign_in(
                phone=p.phone, code=code, phone_code_hash=p.phone_code_hash
            )
        except SessionPasswordNeededError:
            p.state = "password_required"
            return self._public(p)
        except (PhoneCodeInvalidError, PhoneCodeEmptyError):
            p.code_attempts += 1
            if p.code_attempts >= MAX_CODE_ATTEMPTS:
                await self._remove(p.auth_id)
                raise LoginExpired(
                    "驗證碼錯誤次數過多，請重新取得驗證碼"
                ) from None
            raise ValueError("Telegram 驗證碼不正確") from None
        except PhoneCodeExpiredError:
            await self._remove(p.auth_id)
            raise LoginExpired("驗證碼已過期，請重新取得") from None
        except FloodWaitError as e:
            raise LoginRateLimit(
                "驗證嘗試太頻繁，請稍後再試", int(e.seconds)
            ) from None
        except Exception:
            raise LoginRateLimit("Telegram 驗證暫時失敗，請稍後再試") from None
        await self._authorize(p)
        return self._public(p)

    async def submit_password(self, auth_id: object, password_value: object) -> dict:
        p = await self._get(auth_id)
        password = str(password_value or "")
        if not password or len(password) > 512:
            raise ValueError("請輸入 Telegram 兩步驗證密碼")
        if p.state != "password_required":
            raise LoginConflict("目前登入流程不接受密碼")
        try:
            await p.client.sign_in(password=password)
        except PasswordHashInvalidError:
            p.password_attempts += 1
            if p.password_attempts >= MAX_PASSWORD_ATTEMPTS:
                await self._remove(p.auth_id)
                raise LoginExpired(
                    "兩步驗證密碼錯誤次數過多，請重新登入"
                ) from None
            raise ValueError("兩步驗證密碼不正確") from None
        except FloodWaitError as e:
            raise LoginRateLimit(
                "驗證嘗試太頻繁，請稍後再試", int(e.seconds)
            ) from None
        except Exception:
            raise LoginRateLimit("Telegram 驗證暫時失敗，請稍後再試") from None
        await self._authorize(p)
        return self._public(p)

    async def claim(self, auth_id: object) -> VerifiedSession:
        p = await self._get(auth_id)
        if p.state != "authorized" or p.verified is None:
            raise LoginConflict("請先完成 Telegram 驗證")
        if p.claimed:
            raise LoginConflict("帳號正在建立中，請勿重複提交")
        p.claimed = True
        return p.verified

    async def cancel(self, auth_id: object) -> None:
        await self._remove(str(auth_id or "").strip())

    async def prune_expired(self) -> None:
        now = time.time()
        expired = [aid for aid, p in self.pending.items() if now > p.expires_at]
        for aid in expired:
            await self._remove(aid)
