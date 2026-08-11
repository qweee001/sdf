from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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

from .crypto import SecretBox


FLOW_TTL_SECONDS = 10 * 60
AUTHORIZED_TTL_SECONDS = 5 * 60
START_COOLDOWN_SECONDS = 60
MAX_CODE_ATTEMPTS = 5
MAX_PASSWORD_ATTEMPTS = 3


class TelegramLoginExpired(ValueError):
    pass


class TelegramLoginConflict(RuntimeError):
    pass


class TelegramLoginUnavailable(RuntimeError):
    pass


class TelegramLoginRateLimit(RuntimeError):
    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(int(retry_after), 1)


@dataclass(frozen=True, slots=True)
class VerifiedTelegramSession:
    session_ciphertext: str
    session_fingerprint: str
    telegram_user_id: int
    telegram_name: str


@dataclass(slots=True)
class PendingTelegramLogin:
    auth_id: str
    owner_id: str
    phone: str
    phone_hint: str
    phone_code_hash: str
    client: Any
    created_at: float
    expires_at: float
    state: str = "code_required"
    code_attempts: int = 0
    password_attempts: int = 0
    verified: VerifiedTelegramSession | None = None
    claimed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TelegramLoginService:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        secret_box: SecretBox,
        *,
        max_flows: int = 10,
        client_factory: Callable[..., Any] = TelegramClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.secret_box = secret_box
        self.max_flows = max(1, min(max_flows, 20))
        self.client_factory = client_factory
        self.clock = clock
        self.pending: dict[str, PendingTelegramLogin] = {}
        self.last_start: dict[str, float] = {}
        self.last_phone_start: dict[str, float] = {}
        self.owner_generation: dict[str, int] = {}
        self._dict_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

    @staticmethod
    def _clean_owner(owner_id: str) -> str:
        owner = str(owner_id or "").strip()
        if not owner or len(owner) > 200:
            raise TelegramLoginExpired("登入流程不存在或已過期")
        return owner

    @staticmethod
    def _clean_auth_id(auth_id: object) -> str:
        value = str(auth_id or "").strip()
        if not value or len(value) > 200:
            raise TelegramLoginExpired("登入流程不存在或已過期")
        return value

    @staticmethod
    def _clean_phone(value: object) -> str:
        phone = re.sub(r"[\s()\-]", "", str(value or ""))
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
            raise ValueError("請輸入含國碼的手機號碼，例如 +886912345678")
        return phone

    @staticmethod
    def _mask_phone(phone: str) -> str:
        if len(phone) <= 7:
            return f"{phone[:2]}•••{phone[-2:]}"
        return f"{phone[:4]}•••••{phone[-3:]}"

    @staticmethod
    def _clean_code(value: object) -> str:
        code = re.sub(r"\s", "", str(value or ""))
        if not re.fullmatch(r"[0-9]{4,8}", code):
            raise ValueError("請輸入 Telegram 傳送的數字驗證碼")
        return code

    @staticmethod
    def _clean_password(value: object) -> str:
        password = str(value or "")
        if not password or len(password) > 512:
            raise ValueError("請輸入 Telegram 兩步驗證密碼")
        return password

    def _public(self, pending: PendingTelegramLogin) -> dict[str, object]:
        return {
            "auth_id": pending.auth_id,
            "status": pending.state,
            "phone_hint": pending.phone_hint,
            "expires_in": max(int(pending.expires_at - self.clock()), 0),
            "resend_after": START_COOLDOWN_SECONDS,
        }

    async def _disconnect(self, client: Any) -> None:
        try:
            if client is not None and client.is_connected():
                await client.disconnect()
        except Exception:
            pass

    async def _remove(
        self,
        auth_id: str,
        expected: PendingTelegramLogin | None = None,
    ) -> PendingTelegramLogin | None:
        async with self._dict_lock:
            pending = self.pending.get(auth_id)
            if pending is None or (expected is not None and pending is not expected):
                return None
            self.pending.pop(auth_id, None)
        await self._disconnect(pending.client)
        pending.client = None
        pending.phone = ""
        pending.phone_code_hash = ""
        pending.verified = None
        pending.state = "closed"
        return pending

    async def prune_expired(self) -> int:
        now = self.clock()
        async with self._dict_lock:
            candidates = [
                (auth_id, pending)
                for auth_id, pending in self.pending.items()
                if pending.expires_at <= now
            ]
            self.last_start = {
                owner_id: started_at
                for owner_id, started_at in self.last_start.items()
                if now - started_at < FLOW_TTL_SECONDS
            }
            self.last_phone_start = {
                phone_key: started_at
                for phone_key, started_at in self.last_phone_start.items()
                if now - started_at < FLOW_TTL_SECONDS
            }
            active_owners = {
                pending.owner_id for pending in self.pending.values()
            } | set(self.last_start)
            self.owner_generation = {
                owner_id: generation
                for owner_id, generation in self.owner_generation.items()
                if owner_id in active_owners
            }

        removed = 0
        for auth_id, pending in candidates:
            async with pending.lock:
                if pending.claimed:
                    continue
                async with self._dict_lock:
                    is_expired = (
                        self.pending.get(auth_id) is pending
                        and pending.expires_at <= self.clock()
                    )
                if is_expired and await self._remove(auth_id, pending) is not None:
                    removed += 1
        return removed

    async def cancel_owner(self, owner_id: str) -> int:
        owner = self._clean_owner(owner_id)
        async with self._dict_lock:
            self.owner_generation[owner] = self.owner_generation.get(owner, 0) + 1
            matches = [
                (auth_id, pending)
                for auth_id, pending in self.pending.items()
                if pending.owner_id == owner
            ]
        removed = 0
        for auth_id, pending in matches:
            async with pending.lock:
                if pending.claimed:
                    continue
                if await self._remove(auth_id, pending) is not None:
                    removed += 1
        return removed

    async def start(self, owner_id: str, phone_value: object) -> dict[str, object]:
        owner = self._clean_owner(owner_id)
        phone = self._clean_phone(phone_value)
        phone_key = self.secret_box.fingerprint(phone)
        async with self._start_lock:
            await self.prune_expired()
            now = self.clock()
            previous_owner = self.last_start.get(owner)
            previous_phone = self.last_phone_start.get(phone_key)
            prior_starts = [
                value
                for value in (previous_owner, previous_phone)
                if value is not None
            ]
            previous = max(prior_starts) if prior_starts else None
            if previous is not None and now - previous < START_COOLDOWN_SECONDS:
                retry_after = int(START_COOLDOWN_SECONDS - (now - previous)) + 1
                raise TelegramLoginRateLimit(
                    "驗證碼請求太頻繁，請稍後再試",
                    retry_after,
                )
            await self.cancel_owner(owner)
            async with self._dict_lock:
                if len(self.pending) >= self.max_flows:
                    raise TelegramLoginConflict("目前登入流程已滿，請稍後再試")
                generation = self.owner_generation.get(owner, 0)
                self.last_start[owner] = now
                self.last_phone_start[phone_key] = now

            client = self.client_factory(
                StringSession(),
                self.api_id,
                self.api_hash,
            )
            try:
                try:
                    # 修 M13: 网络调用加 30s 超时——卡住的 Telegram 连接
                    # 不能永久阻塞 _start_lock（其他账号登录全串行等待）
                    await asyncio.wait_for(client.connect(), timeout=30)
                    sent = await asyncio.wait_for(
                        client.send_code_request(phone), timeout=30
                    )
                except asyncio.TimeoutError:
                    await self._disconnect(client)
                    raise TelegramLoginUnavailable(
                        "Telegram 連線逾時，請稍後再試"
                    ) from None
                except FloodWaitError as exc:
                    await self._disconnect(client)
                    raise TelegramLoginRateLimit(
                        "Telegram 要求稍後再傳送驗證碼",
                        int(exc.seconds),
                    ) from None
                except PhoneNumberFloodError:
                    await self._disconnect(client)
                    raise TelegramLoginRateLimit(
                        "這個號碼的驗證要求過多，請稍後再試",
                        START_COOLDOWN_SECONDS,
                    ) from None
                except PhoneNumberInvalidError:
                    await self._disconnect(client)
                    raise ValueError(
                        "手機號碼格式無效，請確認國碼與號碼"
                    ) from None
                except PhoneNumberBannedError:
                    await self._disconnect(client)
                    raise ValueError(
                        "這個 Telegram 手機號碼目前無法登入"
                    ) from None
                except Exception:
                    await self._disconnect(client)
                    raise TelegramLoginUnavailable(
                        "Telegram 暫時無法傳送驗證碼，請稍後再試"
                    ) from None

                auth_id = secrets.token_urlsafe(32)
                pending = PendingTelegramLogin(
                    auth_id=auth_id,
                    owner_id=owner,
                    phone=phone,
                    phone_hint=self._mask_phone(phone),
                    phone_code_hash=str(sent.phone_code_hash),
                    client=client,
                    created_at=now,
                    expires_at=now + FLOW_TTL_SECONDS,
                )
                async with self._dict_lock:
                    cancelled = self.owner_generation.get(owner, 0) != generation
                    at_capacity = len(self.pending) >= self.max_flows
                    if not cancelled and not at_capacity:
                        self.pending[auth_id] = pending
                if cancelled:
                    await self._disconnect(client)
                    raise TelegramLoginExpired(
                        "登入流程已取消，請重新取得驗證碼"
                    )
                if at_capacity:
                    await self._disconnect(client)
                    raise TelegramLoginConflict(
                        "目前登入流程已滿，請稍後再試"
                    )
                return self._public(pending)
            except asyncio.CancelledError:
                await asyncio.shield(self._disconnect(client))
                raise

    async def _get(
        self,
        owner_id: str,
        auth_id: object,
    ) -> PendingTelegramLogin:
        owner = self._clean_owner(owner_id)
        cleaned_auth_id = self._clean_auth_id(auth_id)
        async with self._dict_lock:
            pending = self.pending.get(cleaned_auth_id)
        if pending is None or pending.owner_id != owner:
            raise TelegramLoginExpired("登入流程不存在或已過期")
        return pending

    async def _ensure_active_locked(
        self,
        pending: PendingTelegramLogin,
    ) -> None:
        async with self._dict_lock:
            is_current = self.pending.get(pending.auth_id) is pending
        if not is_current:
            raise TelegramLoginExpired("登入流程不存在或已過期")
        if pending.expires_at <= self.clock():
            await self._remove(pending.auth_id, pending)
            raise TelegramLoginExpired("登入流程已過期，請重新取得驗證碼")

    async def _authorize(self, pending: PendingTelegramLogin) -> None:
        me = await pending.client.get_me()
        if me is None:
            raise TelegramLoginUnavailable("無法取得 Telegram 帳號資料")
        session_string = pending.client.session.save()
        if not session_string:
            raise TelegramLoginUnavailable("Telegram Session 建立失敗")
        pending.verified = VerifiedTelegramSession(
            session_ciphertext=self.secret_box.encrypt(session_string),
            session_fingerprint=self.secret_box.fingerprint(session_string),
            telegram_user_id=int(me.id),
            telegram_name=get_display_name(me) or str(me.id),
        )
        pending.state = "authorized"
        pending.phone = ""
        pending.phone_code_hash = ""
        pending.expires_at = min(
            pending.created_at + FLOW_TTL_SECONDS,
            self.clock() + AUTHORIZED_TTL_SECONDS,
        )
        await self._disconnect(pending.client)
        pending.client = None

    async def _finish_authorization(
        self,
        pending: PendingTelegramLogin,
    ) -> None:
        try:
            await self._authorize(pending)
        except asyncio.CancelledError:
            await asyncio.shield(self._remove(pending.auth_id, pending))
            raise
        except Exception:
            await self._remove(pending.auth_id, pending)
            raise TelegramLoginUnavailable(
                "Telegram 登入已完成但 Session 建立失敗，請重新登入"
            ) from None

    async def submit_code(
        self,
        owner_id: str,
        auth_id: object,
        code_value: object,
    ) -> dict[str, object]:
        pending = await self._get(owner_id, auth_id)
        code = self._clean_code(code_value)
        async with pending.lock:
            await self._ensure_active_locked(pending)
            if pending.state != "code_required":
                raise TelegramLoginConflict("目前登入流程不接受驗證碼")
            try:
                await pending.client.sign_in(
                    phone=pending.phone,
                    code=code,
                    phone_code_hash=pending.phone_code_hash,
                )
            except SessionPasswordNeededError:
                pending.state = "password_required"
                return self._public(pending)
            except (PhoneCodeInvalidError, PhoneCodeEmptyError):
                pending.code_attempts += 1
                if pending.code_attempts >= MAX_CODE_ATTEMPTS:
                    await self._remove(pending.auth_id, pending)
                    raise TelegramLoginExpired(
                        "驗證碼錯誤次數過多，請重新取得驗證碼"
                    ) from None
                raise ValueError("Telegram 驗證碼不正確") from None
            except PhoneCodeExpiredError:
                await self._remove(pending.auth_id, pending)
                raise TelegramLoginExpired(
                    "Telegram 驗證碼已過期，請重新取得"
                ) from None
            except FloodWaitError as exc:
                raise TelegramLoginRateLimit(
                    "驗證嘗試太頻繁，請稍後再試",
                    int(exc.seconds),
                ) from None
            except Exception:
                raise TelegramLoginUnavailable(
                    "Telegram 驗證暫時失敗，請稍後再試"
                ) from None
            await self._finish_authorization(pending)
            return self._public(pending)

    async def submit_password(
        self,
        owner_id: str,
        auth_id: object,
        password_value: object,
    ) -> dict[str, object]:
        pending = await self._get(owner_id, auth_id)
        password = self._clean_password(password_value)
        async with pending.lock:
            await self._ensure_active_locked(pending)
            if pending.state != "password_required":
                raise TelegramLoginConflict("目前登入流程不接受兩步驗證密碼")
            try:
                await pending.client.sign_in(password=password)
            except PasswordHashInvalidError:
                pending.password_attempts += 1
                if pending.password_attempts >= MAX_PASSWORD_ATTEMPTS:
                    await self._remove(pending.auth_id, pending)
                    raise TelegramLoginExpired(
                        "兩步驗證密碼錯誤次數過多，請重新登入"
                    ) from None
                raise ValueError("Telegram 兩步驗證密碼不正確") from None
            except FloodWaitError as exc:
                raise TelegramLoginRateLimit(
                    "驗證嘗試太頻繁，請稍後再試",
                    int(exc.seconds),
                ) from None
            except Exception:
                raise TelegramLoginUnavailable(
                    "Telegram 兩步驗證暫時失敗，請稍後再試"
                ) from None
            await self._finish_authorization(pending)
            return self._public(pending)

    async def claim_authorized(
        self,
        owner_id: str,
        auth_id: object,
    ) -> VerifiedTelegramSession:
        pending = await self._get(owner_id, auth_id)
        async with pending.lock:
            await self._ensure_active_locked(pending)
            if pending.state != "authorized" or pending.verified is None:
                raise TelegramLoginConflict("請先完成 Telegram 驗證")
            if pending.claimed:
                raise TelegramLoginConflict("帳號正在建立中，請勿重複提交")
            pending.claimed = True
            return pending.verified

    async def release_claim(self, owner_id: str, auth_id: object) -> None:
        try:
            pending = await self._get(owner_id, auth_id)
        except TelegramLoginExpired:
            return
        async with pending.lock:
            if pending.state == "authorized":
                pending.claimed = False
                if pending.expires_at <= self.clock():
                    await self._remove(pending.auth_id, pending)

    async def complete(self, owner_id: str, auth_id: object) -> None:
        pending = await self._get(owner_id, auth_id)
        async with pending.lock:
            await self._remove(pending.auth_id, pending)

    async def cancel(self, owner_id: str, auth_id: object) -> None:
        pending = await self._get(owner_id, auth_id)
        async with pending.lock:
            await self._ensure_active_locked(pending)
            if pending.claimed:
                raise TelegramLoginConflict("帳號正在建立中，無法取消")
            await self._remove(pending.auth_id, pending)

    async def close(self) -> None:
        async with self._dict_lock:
            pending = list(self.pending.values())
            self.pending.clear()
            self.last_start.clear()
            self.last_phone_start.clear()
            self.owner_generation.clear()
        for item in pending:
            async with item.lock:
                await self._disconnect(item.client)
                item.client = None
                item.phone = ""
                item.phone_code_hash = ""
                item.verified = None
                item.state = "closed"
