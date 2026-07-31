from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from typing import Any

from openai import AsyncOpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.utils import get_display_name

from .account import (
    AccountRecord,
    clean_bool,
    clean_float,
    clean_group_ids,
    clean_int,
    clean_role,
    clean_text,
    validate_provider_url,
)
from .config import Settings
from .crypto import SecretBox
from .memory import MemoryStore
from .security import safe_error
from .telegram_login import TelegramLoginService, VerifiedTelegramSession
from .worker import AccountWorker


LOGGER = logging.getLogger("telegram-ai-userbot.manager")


class AccountNotFoundError(ValueError):
    pass


class AccountConflictError(RuntimeError):
    pass


class AccountManager:
    def __init__(
        self,
        settings: Settings,
        store: MemoryStore,
        secrets: SecretBox,
        telegram_login: TelegramLoginService | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.secrets = secrets
        self.telegram_login = telegram_login or TelegramLoginService(
            settings.tg_api_id,
            settings.tg_api_hash,
            secrets,
            max_flows=min(settings.max_accounts * 2, 20),
        )
        self.workers: dict[str, AccountWorker] = {}
        self.worker_tasks: dict[str, asyncio.Task[None]] = {}
        self.start_errors: dict[str, str] = {}
        self._managed_ids: frozenset[int] = frozenset()
        self._lock = asyncio.Lock()
        self._create_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._retry_attempts: dict[str, int] = {}
        self._next_retry_at: dict[str, float] = {}

    def managed_ids(self) -> frozenset[int]:
        return self._managed_ids

    async def start(self) -> None:
        await self.store.open()
        await self._import_legacy_account()
        await self._refresh_managed_ids()
        accounts = await self.store.list_accounts()
        for account in accounts:
            if account.enabled:
                await self.start_account(account.id)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())

    async def _import_legacy_account(self) -> None:
        if await self.store.count_accounts() or not self.settings.legacy_session_string:
            return
        fingerprint = self.secrets.fingerprint(self.settings.legacy_session_string)
        try:
            telegram_user_id, telegram_name = await self.verify_session(
                self.settings.legacy_session_string
            )
        except Exception as exc:
            telegram_user_id = -(int(fingerprint[:15], 16) + 1)
            telegram_name = "待驗證的既有帳號"
            LOGGER.warning(
                "Legacy Telegram Session could not be verified during migration; "
                "the dashboard will still start and the worker will retry: %s",
                self._safe_error(exc),
            )
        runtime = await self.store.legacy_runtime_settings()
        enabled = runtime.get("ai_enabled", "true").lower() == "true"
        all_groups = not bool(self.settings.legacy_group_ids)
        group_ids = self.settings.legacy_group_ids
        if runtime.get("group_mode") in {"all", "selected"}:
            all_groups = runtime["group_mode"] == "all"
            try:
                group_ids = frozenset(
                    int(item) for item in json.loads(runtime.get("group_ids", "[]"))
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                group_ids = frozenset()
        now = int(time.time())
        record = AccountRecord(
            id="primary",
            label=telegram_name or "主要帳號",
            session_ciphertext=self.secrets.encrypt(
                self.settings.legacy_session_string
            ),
            session_fingerprint=fingerprint,
            telegram_user_id=telegram_user_id,
            telegram_name=telegram_name,
            enabled=enabled,
            gender=self.settings.legacy_gender,
            stage=self.settings.legacy_stage,
            style=self.settings.legacy_style,
            task_name="一般群聊互動",
            task_info="依照群內話題自然接話，並遵守成年、自願、尊重與隱私原則。",
            ai_base_url=validate_provider_url(self.settings.ai_base_url),
            ai_model=self.settings.ai_model,
            ai_api_key_ciphertext="",
            group_reply_probability=self.settings.legacy_group_reply_probability,
            reply_on_mention=self.settings.legacy_reply_on_mention,
            reply_on_reply=self.settings.legacy_reply_on_reply,
            typing_delay_min_seconds=self.settings.legacy_typing_delay_min_seconds,
            typing_delay_max_seconds=self.settings.legacy_typing_delay_max_seconds,
            proactive_enabled=self.settings.legacy_proactive_enabled,
            proactive_idle_minutes=self.settings.legacy_proactive_idle_minutes,
            proactive_min_interval_minutes=(
                self.settings.legacy_proactive_min_interval_minutes
            ),
            proactive_max_interval_minutes=(
                self.settings.legacy_proactive_max_interval_minutes
            ),
            max_proactive_per_day=self.settings.legacy_max_proactive_per_day,
            all_groups=all_groups,
            group_ids=group_ids,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        await self.store.create_account(record)
        LOGGER.info("Imported the existing Telegram account into multi-account storage")

    async def verify_session(self, session_string: str) -> tuple[int, str]:
        session = clean_text(
            session_string,
            "session_string",
            maximum=12000,
            required=True,
        )
        client = TelegramClient(
            StringSession(session),
            self.settings.tg_api_id,
            self.settings.tg_api_hash,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError("TG_SESSION_STRING 已失效或尚未完成登入")
            me = await client.get_me()
            if me is None:
                raise ValueError("無法從 TG_SESSION_STRING 取得 Telegram 帳號")
            return int(me.id), get_display_name(me) or str(me.id)
        finally:
            if client.is_connected():
                await client.disconnect()

    async def _refresh_managed_ids(self) -> None:
        accounts = await self.store.list_accounts()
        self._managed_ids = frozenset(
            {account.telegram_user_id for account in accounts}
            | set(self.settings.legacy_ignore_sender_ids)
        )

    async def start_account(self, account_id: str) -> None:
        async with self._lock:
            existing = self.worker_tasks.get(account_id)
            if existing is not None and not existing.done():
                return
            account = await self._require_account(account_id)
            if not account.enabled:
                return
            self.start_errors.pop(account_id, None)
            try:
                worker = AccountWorker(
                    self.settings,
                    account,
                    self.secrets,
                    self.store,
                    self.managed_ids,
                    self._record_identity,
                )
            except Exception as exc:
                self.start_errors[account_id] = self._safe_error(exc)
                LOGGER.error(
                    "Account %s could not create its worker: %s",
                    account_id,
                    self._safe_error(exc),
                )
                return
            self.workers[account_id] = worker
            self.worker_tasks[account_id] = asyncio.create_task(worker.run())

    async def stop_account(self, account_id: str) -> None:
        async with self._lock:
            worker = self.workers.get(account_id)
            task = self.worker_tasks.get(account_id)
            if worker is not None:
                await worker.close()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.workers.pop(account_id, None)
            self.worker_tasks.pop(account_id, None)
            if worker is not None or not (await self._require_account(account_id)).enabled:
                self.start_errors.pop(account_id, None)

    async def restart_account(self, account_id: str) -> dict[str, object]:
        account = await self._require_account(account_id)
        self._retry_attempts.pop(account_id, None)
        self._next_retry_at.pop(account_id, None)
        await self.stop_account(account_id)
        if account.enabled:
            await self.start_account(account_id)
        return await self.account_status(account_id)

    async def start_phone_login(
        self,
        owner_id: str,
        phone: object,
    ) -> dict[str, object]:
        return await self.telegram_login.start(owner_id, phone)

    async def submit_phone_code(
        self,
        owner_id: str,
        auth_id: object,
        code: object,
    ) -> dict[str, object]:
        return await self.telegram_login.submit_code(owner_id, auth_id, code)

    async def submit_phone_password(
        self,
        owner_id: str,
        auth_id: object,
        password: object,
    ) -> dict[str, object]:
        return await self.telegram_login.submit_password(
            owner_id,
            auth_id,
            password,
        )

    async def cancel_phone_login(self, owner_id: str, auth_id: object) -> None:
        await self.telegram_login.cancel(owner_id, auth_id)

    async def cancel_phone_logins_for_owner(self, owner_id: str) -> int:
        return await self.telegram_login.cancel_owner(owner_id)

    async def create_account(
        self,
        payload: dict[str, Any],
        owner_id: str | None = None,
    ) -> dict[str, object]:
        if await self.store.count_accounts() >= self.settings.max_accounts:
            raise ValueError(f"最多可管理 {self.settings.max_accounts} 個帳號")
        session_string = clean_text(
            payload.get("session_string"),
            "session_string",
            maximum=12000,
        )
        auth_id = clean_text(
            payload.get("telegram_auth_id"),
            "telegram_auth_id",
            maximum=200,
        )
        if bool(session_string) == bool(auth_id):
            raise ValueError(
                "請使用手機驗證登入，或提供一個 TG_SESSION_STRING"
            )

        if auth_id:
            if not owner_id:
                raise ValueError("手機驗證登入只能從控制台完成")
            verified = await self.telegram_login.claim_authorized(owner_id, auth_id)
            try:
                result = await self._create_verified_account(payload, verified)
            except Exception:
                await self.telegram_login.release_claim(owner_id, auth_id)
                raise
            await self.telegram_login.complete(owner_id, auth_id)
            return result

        telegram_user_id, telegram_name = await self.verify_session(session_string)
        verified = VerifiedTelegramSession(
            session_ciphertext=self.secrets.encrypt(session_string),
            session_fingerprint=self.secrets.fingerprint(session_string),
            telegram_user_id=telegram_user_id,
            telegram_name=telegram_name,
        )
        return await self._create_verified_account(payload, verified)

    async def _create_verified_account(
        self,
        payload: dict[str, Any],
        verified: VerifiedTelegramSession,
    ) -> dict[str, object]:
        gender, stage = clean_role(
            payload.get("gender", "male"),
            payload.get("stage", "observer"),
        )
        now = int(time.time())
        ai_key = clean_text(payload.get("ai_api_key"), "ai_api_key", maximum=12000)
        record = AccountRecord(
            id=f"acct_{uuid.uuid4().hex[:12]}",
            label=clean_text(
                payload.get("label") or verified.telegram_name,
                "label",
                maximum=60,
                required=True,
            ),
            session_ciphertext=verified.session_ciphertext,
            session_fingerprint=verified.session_fingerprint,
            telegram_user_id=verified.telegram_user_id,
            telegram_name=verified.telegram_name,
            enabled=clean_bool(payload.get("enabled", True), "enabled"),
            gender=gender,
            stage=stage,
            style=clean_text(payload.get("style"), "style", maximum=500),
            task_name=clean_text(
                payload.get("task_name") or "一般群聊互動",
                "task_name",
                maximum=120,
                required=True,
            ),
            task_info=clean_text(payload.get("task_info"), "task_info", maximum=3000),
            ai_base_url=validate_provider_url(
                payload.get("ai_base_url") or self.settings.ai_base_url
            ),
            ai_model=clean_text(
                payload.get("ai_model") or self.settings.ai_model,
                "ai_model",
                maximum=200,
                required=True,
            ),
            ai_api_key_ciphertext=self.secrets.encrypt(ai_key) if ai_key else "",
            group_reply_probability=clean_float(
                payload.get("group_reply_probability", 0.35),
                "group_reply_probability",
                minimum=0,
                maximum=1,
            ),
            reply_on_mention=clean_bool(
                payload.get("reply_on_mention", True),
                "reply_on_mention",
            ),
            reply_on_reply=clean_bool(
                payload.get("reply_on_reply", True),
                "reply_on_reply",
            ),
            typing_delay_min_seconds=clean_float(
                payload.get("typing_delay_min_seconds", 1.5),
                "typing_delay_min_seconds",
                minimum=0,
                maximum=60,
            ),
            typing_delay_max_seconds=clean_float(
                payload.get("typing_delay_max_seconds", 5),
                "typing_delay_max_seconds",
                minimum=0,
                maximum=60,
            ),
            proactive_enabled=clean_bool(
                payload.get("proactive_enabled", True),
                "proactive_enabled",
            ),
            proactive_idle_minutes=clean_int(
                payload.get("proactive_idle_minutes", 15),
                "proactive_idle_minutes",
                minimum=1,
                maximum=1440,
            ),
            proactive_min_interval_minutes=clean_int(
                payload.get("proactive_min_interval_minutes", 25),
                "proactive_min_interval_minutes",
                minimum=1,
                maximum=1440,
            ),
            proactive_max_interval_minutes=clean_int(
                payload.get("proactive_max_interval_minutes", 60),
                "proactive_max_interval_minutes",
                minimum=1,
                maximum=1440,
            ),
            max_proactive_per_day=clean_int(
                payload.get("max_proactive_per_day", 24),
                "max_proactive_per_day",
                minimum=0,
                maximum=200,
            ),
            all_groups=True,
            group_ids=frozenset(),
            revision=1,
            created_at=now,
            updated_at=now,
        )
        self._validate_intervals(record)
        if not record.has_custom_api_key and not self.settings.ai_api_key:
            raise ValueError("請填入帳號專用 AI API Key")
        async with self._create_lock:
            if await self.store.count_accounts() >= self.settings.max_accounts:
                raise ValueError(
                    f"最多可管理 {self.settings.max_accounts} 個帳號"
                )
            try:
                await self.store.create_account(record)
            except Exception as exc:
                if "UNIQUE constraint failed" in str(exc):
                    raise AccountConflictError("這個 Telegram 帳號已經存在") from exc
                raise
        await self.store.adopt_legacy_messages(record.id)
        await self._refresh_managed_ids()
        if record.enabled:
            await self.start_account(record.id)
        return await self.account_status(record.id)

    async def update_account(
        self,
        account_id: str,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        current = await self._require_account(account_id)
        revision = clean_int(
            payload.get("revision"),
            "revision",
            minimum=1,
            maximum=2_000_000_000,
        )
        if revision != current.revision:
            raise AccountConflictError("設定已被其他操作更新，請重新整理")

        gender, stage = clean_role(
            payload.get("gender", current.gender),
            payload.get("stage", current.stage),
        )
        session_ciphertext = current.session_ciphertext
        session_fingerprint = current.session_fingerprint
        telegram_user_id = current.telegram_user_id
        telegram_name = current.telegram_name
        new_session = clean_text(
            payload.get("session_string"),
            "session_string",
            maximum=12000,
        )
        if new_session:
            telegram_user_id, telegram_name = await self.verify_session(new_session)
            session_ciphertext = self.secrets.encrypt(new_session)
            session_fingerprint = self.secrets.fingerprint(new_session)

        ai_key_ciphertext = current.ai_api_key_ciphertext
        new_ai_key = clean_text(payload.get("ai_api_key"), "ai_api_key", maximum=12000)
        if new_ai_key:
            ai_key_ciphertext = self.secrets.encrypt(new_ai_key)
        elif payload.get("clear_ai_api_key") is True:
            ai_key_ciphertext = ""

        updated = current.with_updates(
            label=clean_text(
                payload.get("label", current.label),
                "label",
                maximum=60,
                required=True,
            ),
            session_ciphertext=session_ciphertext,
            session_fingerprint=session_fingerprint,
            telegram_user_id=telegram_user_id,
            telegram_name=telegram_name,
            gender=gender,
            stage=stage,
            style=clean_text(
                payload.get("style", current.style),
                "style",
                maximum=500,
            ),
            task_name=clean_text(
                payload.get("task_name", current.task_name),
                "task_name",
                maximum=120,
                required=True,
            ),
            task_info=clean_text(
                payload.get("task_info", current.task_info),
                "task_info",
                maximum=3000,
            ),
            ai_base_url=validate_provider_url(
                payload.get("ai_base_url", current.ai_base_url)
            ),
            ai_model=clean_text(
                payload.get("ai_model", current.ai_model),
                "ai_model",
                maximum=200,
                required=True,
            ),
            ai_api_key_ciphertext=ai_key_ciphertext,
            group_reply_probability=clean_float(
                payload.get(
                    "group_reply_probability",
                    current.group_reply_probability,
                ),
                "group_reply_probability",
                minimum=0,
                maximum=1,
            ),
            reply_on_mention=clean_bool(
                payload.get("reply_on_mention", current.reply_on_mention),
                "reply_on_mention",
            ),
            reply_on_reply=clean_bool(
                payload.get("reply_on_reply", current.reply_on_reply),
                "reply_on_reply",
            ),
            typing_delay_min_seconds=clean_float(
                payload.get(
                    "typing_delay_min_seconds",
                    current.typing_delay_min_seconds,
                ),
                "typing_delay_min_seconds",
                minimum=0,
                maximum=60,
            ),
            typing_delay_max_seconds=clean_float(
                payload.get(
                    "typing_delay_max_seconds",
                    current.typing_delay_max_seconds,
                ),
                "typing_delay_max_seconds",
                minimum=0,
                maximum=60,
            ),
            proactive_enabled=clean_bool(
                payload.get("proactive_enabled", current.proactive_enabled),
                "proactive_enabled",
            ),
            proactive_idle_minutes=clean_int(
                payload.get(
                    "proactive_idle_minutes",
                    current.proactive_idle_minutes,
                ),
                "proactive_idle_minutes",
                minimum=1,
                maximum=1440,
            ),
            proactive_min_interval_minutes=clean_int(
                payload.get(
                    "proactive_min_interval_minutes",
                    current.proactive_min_interval_minutes,
                ),
                "proactive_min_interval_minutes",
                minimum=1,
                maximum=1440,
            ),
            proactive_max_interval_minutes=clean_int(
                payload.get(
                    "proactive_max_interval_minutes",
                    current.proactive_max_interval_minutes,
                ),
                "proactive_max_interval_minutes",
                minimum=1,
                maximum=1440,
            ),
            max_proactive_per_day=clean_int(
                payload.get(
                    "max_proactive_per_day",
                    current.max_proactive_per_day,
                ),
                "max_proactive_per_day",
                minimum=0,
                maximum=200,
            ),
        )
        self._validate_intervals(updated)
        if not updated.has_custom_api_key and not self.settings.ai_api_key:
            raise ValueError("請填入帳號專用 AI API Key")

        changed_fields = [
            name
            for name in (
                "label",
                "session_string",
                "gender",
                "stage",
                "style",
                "task_name",
                "task_info",
                "ai_base_url",
                "ai_model",
                "group_reply_probability",
                "reply_on_mention",
                "reply_on_reply",
                "typing_delay_min_seconds",
                "typing_delay_max_seconds",
                "proactive_enabled",
                "proactive_idle_minutes",
                "proactive_min_interval_minutes",
                "proactive_max_interval_minutes",
                "max_proactive_per_day",
            )
            if name in payload
        ]
        if new_ai_key or payload.get("clear_ai_api_key") is True:
            changed_fields.append("ai_api_key")
        try:
            saved = await self.store.update_account(
                updated,
                expected_revision=current.revision,
                changed_fields=changed_fields,
            )
        except RuntimeError as exc:
            raise AccountConflictError(str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise AccountConflictError("這個 Telegram 帳號已經存在") from exc
        await self._refresh_managed_ids()
        await self.stop_account(account_id)
        if saved.enabled:
            await self.start_account(account_id)
        return await self.account_status(account_id)

    async def set_enabled(
        self,
        account_id: str,
        enabled: bool,
        revision: int,
    ) -> dict[str, object]:
        enabled = clean_bool(enabled, "enabled")
        revision = clean_int(revision, "revision", minimum=1, maximum=2_000_000_000)
        current = await self._require_account(account_id)
        if revision != current.revision:
            raise AccountConflictError("設定已被其他操作更新，請重新整理")
        try:
            saved = await self.store.update_account(
                current.with_updates(enabled=enabled),
                expected_revision=current.revision,
                changed_fields=["enabled"],
            )
        except RuntimeError as exc:
            raise AccountConflictError(str(exc)) from exc
        if enabled:
            self._retry_attempts.pop(saved.id, None)
            self._next_retry_at.pop(saved.id, None)
            await self.start_account(saved.id)
        else:
            self._retry_attempts.pop(saved.id, None)
            self._next_retry_at.pop(saved.id, None)
            await self.stop_account(saved.id)
        return await self.account_status(saved.id)

    async def set_groups(
        self,
        account_id: str,
        all_groups: bool,
        group_ids: list[int],
        revision: int,
    ) -> dict[str, object]:
        all_groups = clean_bool(all_groups, "all_groups")
        revision = clean_int(revision, "revision", minimum=1, maximum=2_000_000_000)
        current = await self._require_account(account_id)
        if revision != current.revision:
            raise AccountConflictError("設定已被其他操作更新，請重新整理")
        cleaned_ids = clean_group_ids(group_ids)
        worker = self.workers.get(account_id)
        if worker is not None and worker.joined_groups:
            joined = frozenset(int(group["id"]) for group in worker.joined_groups)
            cleaned_ids = frozenset(item for item in cleaned_ids if item in joined)
        try:
            saved = await self.store.update_account(
                current.with_updates(all_groups=all_groups, group_ids=cleaned_ids),
                expected_revision=current.revision,
                changed_fields=["all_groups", "group_ids"],
            )
        except RuntimeError as exc:
            raise AccountConflictError(str(exc)) from exc
        await self.stop_account(account_id)
        if saved.enabled:
            await self.start_account(account_id)
        return await self.account_status(account_id)

    async def test_model(self, account_id: str) -> dict[str, object]:
        worker = self.workers.get(account_id)
        if worker is not None and worker.state == "online":
            try:
                return await worker.test_model()
            except Exception as exc:
                return {"ok": False, "error": self._safe_error(exc)}

        account = await self._require_account(account_id)
        custom_key = (
            self.secrets.decrypt(account.ai_api_key_ciphertext)
            if account.ai_api_key_ciphertext
            else ""
        )
        key = custom_key or self.settings.ai_api_key
        if not key:
            return {"ok": False, "error": "尚未設定 AI API Key"}
        client = AsyncOpenAI(
            api_key=key,
            base_url=account.ai_base_url,
            timeout=30,
            max_retries=0,
        )
        started = time.monotonic()
        try:
            result = await client.chat.completions.create(
                model=account.ai_model,
                messages=[{"role": "user", "content": "只回覆：連線正常"}],
            )
            content = (result.choices[0].message.content or "").strip()
            return {
                "ok": bool(content),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "response": content[:30],
            }
        except Exception as exc:
            return {"ok": False, "error": self._safe_error(exc)}
        finally:
            await client.close()

    async def clear_memory(self, account_id: str) -> int:
        await self._require_account(account_id)
        worker = self.workers.get(account_id)
        if worker is not None:
            worker.last_activity.clear()
            worker.last_proactive.clear()
            worker.next_proactive_interval.clear()
            worker.proactive_counts.clear()
        return await self.store.clear_all(account_id)

    async def account_status(self, account_id: str) -> dict[str, object]:
        account = await self._require_account(account_id)
        worker = self.workers.get(account_id)
        if worker is not None:
            status = await worker.status()
            status["enabled"] = account.enabled
            status["revision"] = account.revision
            return status
        stats = await self.store.statistics(account_id)
        return {
            **account.public_dict(),
            "state": (
                "disabled"
                if not account.enabled
                else "error"
                if account_id in self.start_errors
                else "stopped"
            ),
            "connected": False,
            "joined_groups": [],
            "message_count": stats["message_count"],
            "group_count": stats["group_count"],
            "replies_sent": 0,
            "errors": 1 if account_id in self.start_errors else 0,
            "last_error": self.start_errors.get(account_id, ""),
            "uptime_seconds": 0,
        }

    async def status(self) -> dict[str, object]:
        accounts = await self.store.list_accounts()
        statuses = [await self.account_status(account.id) for account in accounts]
        connected = sum(1 for item in statuses if item["connected"])
        enabled = sum(1 for item in statuses if item["enabled"])
        return {
            "accounts": statuses,
            "summary": {
                "total": len(statuses),
                "enabled": enabled,
                "connected": connected,
                "max_accounts": self.settings.max_accounts,
                "memory_ttl_hours": self.settings.memory_ttl_hours,
            },
        }

    async def _require_account(self, account_id: str) -> AccountRecord:
        account = await self.store.get_account(account_id)
        if account is None:
            raise AccountNotFoundError("找不到帳號")
        return account

    async def _record_identity(
        self,
        account_id: str,
        telegram_user_id: int,
        telegram_name: str,
    ) -> None:
        for _ in range(2):
            current = await self._require_account(account_id)
            if current.telegram_user_id > 0:
                if current.telegram_user_id != telegram_user_id:
                    raise AccountConflictError(
                        "Telegram Session 與已儲存的帳號身分不一致"
                    )
                return
            try:
                await self.store.update_account(
                    current.with_updates(
                        telegram_user_id=telegram_user_id,
                        telegram_name=telegram_name,
                    ),
                    expected_revision=current.revision,
                    changed_fields=["telegram_identity"],
                )
                await self._refresh_managed_ids()
                return
            except RuntimeError:
                continue
        raise AccountConflictError("帳號身分更新衝突，請重新啟動帳號")

    @staticmethod
    def _validate_intervals(account: AccountRecord) -> None:
        if account.typing_delay_max_seconds < account.typing_delay_min_seconds:
            raise ValueError("最大輸入延遲不可小於最小延遲")
        if (
            account.proactive_max_interval_minutes
            < account.proactive_min_interval_minutes
        ):
            raise ValueError("主動發言最大間隔不可小於最小間隔")

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                removed = await self.store.purge_expired()
                if removed:
                    LOGGER.info("Removed %s expired memory rows", removed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.error(
                    "Memory cleanup failed and will be retried: %s",
                    self._safe_error(exc),
                )

    async def _supervisor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(15)
                now = time.monotonic()
                accounts = await self.store.list_accounts()
                for account in accounts:
                    if not account.enabled:
                        self._retry_attempts.pop(account.id, None)
                        self._next_retry_at.pop(account.id, None)
                        continue
                    worker = self.workers.get(account.id)
                    task = self.worker_tasks.get(account.id)
                    if (
                        worker is not None
                        and worker.state == "online"
                        and task is not None
                        and not task.done()
                    ):
                        self._retry_attempts.pop(account.id, None)
                        self._next_retry_at.pop(account.id, None)
                        continue
                    failed = (
                        account.id in self.start_errors
                        or task is None
                        or task.done()
                        or (worker is not None and worker.state == "error")
                    )
                    if not failed:
                        continue
                    deadline = self._next_retry_at.get(account.id)
                    if deadline is None:
                        attempt = self._retry_attempts.get(account.id, 0)
                        delay = min(30 * (2 ** min(attempt, 5)), 15 * 60)
                        self._next_retry_at[account.id] = now + delay
                        LOGGER.warning(
                            "Account %s will retry in %s seconds",
                            account.id,
                            delay,
                        )
                        continue
                    if now < deadline:
                        continue
                    self._next_retry_at.pop(account.id, None)
                    self._retry_attempts[account.id] = (
                        self._retry_attempts.get(account.id, 0) + 1
                    )
                    await self.stop_account(account.id)
                    await self.start_account(account.id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.error(
                    "Account supervisor failed and will continue: %s",
                    self._safe_error(exc),
                )

    async def close(self) -> None:
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            await asyncio.gather(self._supervisor_task, return_exceptions=True)
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
        for account_id in list(self.workers):
            await self.stop_account(account_id)
        await self.telegram_login.close()
        self._retry_attempts.clear()
        self._next_retry_at.clear()
        await self.store.close()

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return safe_error(exc)
