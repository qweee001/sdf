"""
帳號管理器 - 管理所有水軍帳號的生命週期
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack

from openai import AsyncOpenAI
from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.sessions import StringSession

from .config import Settings
from .crypto import SecretBox
from .database import Database
from .live_test import BoundedLiveTest
from .media import OrcaMediaService
from .persona import generate_persona
from .worker import AccountWorker, FIXED_ACCOUNT_PERSONA_AGES


class AccountManager:
    def __init__(self, config: Settings, db: Database, secret_box: SecretBox):
        self.config = config
        self.db = db
        self.secret_box = secret_box
        self.workers: dict[str, AccountWorker] = {}
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}
        self._feature_lock = asyncio.Lock()
        self._voice_library = None
        self.managed_ids: set[int] = set()
        self.active_ids: set[int] = set()
        self.active_group_ids: dict[int, set[int]] = {}
        self.managed_origins: dict[tuple[int, int, str], float] = {}
        self.human_owners: dict[tuple[int, int], tuple[int, float]] = {}
        self.recent_proactive_owners: dict[int, tuple[int, float]] = {}
        self.last_human_activity: dict[int, float] = {}
        self.reply_claim_signals: dict[tuple[int, int], asyncio.Event] = {}
        self.failed_reply_claimants: dict[tuple[int, int], set[int]] = {}
        self._status: dict[str, dict] = {}
        self._ai_client = AsyncOpenAI(
            base_url=config.ai_base_url,
            api_key=config.ai_api_key or "sk-placeholder",
            timeout=config.ai_timeout,
        )
        self._media_service = OrcaMediaService(
            client=self._ai_client,
            db=db,
            config=config,
        )
        enabled_values = {"1", "true", "yes", "on"}
        self.live_test = BoundedLiveTest(
            self,
            enabled=os.getenv("LIVE_TEST_ENABLED", "").strip().lower()
            in enabled_values,
            wan22_ready=os.getenv("LIVE_TEST_WAN22_READY", "").strip().lower()
            in enabled_values,
            asset_root=os.getenv("LIVE_TEST_ASSET_ROOT", "").strip() or None,
        )

    def _lifecycle_lock(self, account_id: str) -> asyncio.Lock:
        """Serialize lifecycle mutations for one account."""
        locks = getattr(self, "_lifecycle_locks", None)
        if locks is None:
            locks = {}
            self._lifecycle_locks = locks
        return locks.setdefault(account_id, asyncio.Lock())

    async def _fixed_persona_mutation_blocked(self, account_id: str) -> bool:
        """Deny fixed-persona writes for every active/reconciliation run state."""
        if account_id not in FIXED_ACCOUNT_PERSONA_AGES:
            return False
        gate = getattr(self.live_test, "outbound_gate", None)
        active = getattr(gate, "_active", None)
        if isinstance(active, tuple) and len(active) == 5:
            # Lockdown intentionally replaces the account set with an empty set,
            # so any active gate blocks every fixed-account persona mutation.
            return True
        finder = getattr(self.db, "get_live_test_reconciliation_run", None)
        if not callable(finder):
            return False
        try:
            latest = await finder()
        except Exception:
            return True
        if not isinstance(latest, dict) or latest.get("status") not in {
            "running",
            "lockdown",
            "needs_reconciliation",
        }:
            return False
        account_ids = {str(value) for value in latest.get("account_ids", [])}
        return account_id in account_ids

    @staticmethod
    def _stored_bool(raw: str | None, default: bool) -> bool:
        if raw is None:
            return default
        return str(raw).strip() == "1"

    def feature_status(self) -> dict[str, bool]:
        voice_available = self._voice_realtime_available()
        return {
            "media_enabled": bool(self.config.media_enabled),
            "voice_enabled": bool(self.config.voice_media_enabled and voice_available),
            "voice_available": voice_available,
        }

    def _voice_realtime_available(self) -> bool:
        return bool(
            str(getattr(self.config, "voice_realtime_url", "") or "").strip()
            and str(getattr(self.config, "voice_realtime_token", "") or "").strip()
        )

    async def load_runtime_settings(self) -> None:
        await self.live_test.reconcile()
        stored = await self.db.get_runtime_settings()
        self.config.media_enabled = self._stored_bool(
            stored.get("media_enabled"), bool(self.config.media_enabled)
        )
        if stored.get("voice_media_enabled") == "1" and self._voice_realtime_available():
            self.config.voice_media_enabled = True
        else:
            self.config.voice_media_enabled = False
            if stored.get("voice_media_enabled") == "1":
                await self.db.set_runtime_settings({"voice_media_enabled": "0"})

    async def update_feature_flags(
        self, *, media_enabled: bool, voice_enabled: bool
    ) -> str:
        if voice_enabled and not self._voice_realtime_available():
            return "即時 IndexTTS2 URL/token 未就緒，語音功能不能開啟"
        async with self._feature_lock:
            await self.db.set_runtime_settings({
                "media_enabled": "1" if media_enabled else "0",
                "voice_media_enabled": "1" if voice_enabled else "0",
            })
            # 所有 worker 共用同一 Settings 物件，更新後立即熱生效。
            self.config.media_enabled = bool(media_enabled)
            self.config.voice_media_enabled = (
                bool(voice_enabled) and self._voice_realtime_available()
            )
        return ""

    @staticmethod
    def _parse_groups(raw_groups: object) -> list[int]:
        """Return a non-empty validated group list, otherwise deny all."""
        if not isinstance(raw_groups, str) or not raw_groups.strip():
            return []
        try:
            raw = json.loads(raw_groups)
            if not isinstance(raw, list):
                return []
            parsed = {int(group_id) for group_id in raw}
            if any(group_id >= 0 for group_id in parsed):
                return []
            return sorted(parsed)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    async def _on_status_change(self, account_id: str, state: str,
                                tg_user_id: int | None, detail: str):
        self._status[account_id] = {"state": state, "detail": detail}

    async def _refresh_managed_ids(self) -> list[dict]:
        accounts = await self.db.list_accounts()
        for acc in accounts:
            tg_user_id = int(acc.get("tg_user_id") or 0)
            if tg_user_id:
                self.managed_ids.add(tg_user_id)
        return accounts

    async def start_all(self):
        """部署重啟後恢復所有 enabled 帳號"""
        blocked = await self.live_test.start_block_error()
        if blocked:
            return blocked
        for acc in await self._refresh_managed_ids():
            if acc.get("enabled"):
                await self._start_account(acc, require_enabled=True)
        return ""

    async def _start_account(
        self, acc: dict, *, require_enabled: bool = False
    ) -> None:
        account_id = str(acc["id"])
        async with self._lifecycle_lock(account_id):
            if await self.live_test.start_block_error():
                return
            if require_enabled:
                persisted = await self.db.get_account(account_id)
                if persisted is not None:
                    if not int(persisted.get("enabled") or 0):
                        return
                    acc = persisted
            await self._start_account_unlocked(acc)

    async def _start_account_unlocked(self, acc: dict) -> None:
        account_id = acc["id"]
        if account_id in self.workers:
            return
        session_key = self.secret_box.decrypt(acc["session_key"])
        persona = None
        if acc.get("persona"):
            try:
                persona = json.loads(acc["persona"])
            except Exception:
                persona = None
        selected_groups = self._parse_groups(acc.get("groups"))
        if not selected_groups:
            await self.db.update_account(
                account_id, enabled=0, setup_complete=0
            )
            return
        # 進程重啟後 last_human_activity 為空 → 主動話題會無視真人活躍直接開炮；
        # 啟動任何帳號前先從持久化事件回填降頻記憶。
        if not self.last_human_activity:
            try:
                backfill = await self.db.last_human_activity_by_group()
                self.last_human_activity.update(backfill)
            except Exception as exc:
                print(f"[manager] last_human_activity backfill error: {exc}", flush=True)
        worker = AccountWorker(
            account_id=account_id,
            session_key=session_key,
            tg_api_id=self.config.tg_api_id,
            tg_api_hash=self.config.tg_api_hash,
            ai_client=self._ai_client,
            db=self.db,
            config=self.config,
            managed_ids=self.managed_ids,
            on_status_change=self._on_status_change,
            persona=persona,
            selected_groups=selected_groups,
            media_service=self._media_service,
            active_ids=self.active_ids,
            active_group_ids=self.active_group_ids,
            managed_origins=self.managed_origins,
            human_owners=self.human_owners,
            recent_proactive_owners=self.recent_proactive_owners,
            last_human_activity=self.last_human_activity,
            reply_claim_signals=self.reply_claim_signals,
            failed_reply_claimants=self.failed_reply_claimants,
            voice_library=None,
            outbound_gate=(
                self.live_test.outbound_gate
                if getattr(self, "live_test", None) is not None
                else None
            ),
        )
        self.workers[account_id] = worker
        await worker.start()
        if not worker.is_running:
            self.workers.pop(account_id, None)

    async def _used_cities(self, exclude_id: str | None = None) -> list[str]:
        used = []
        for a in await self.db.list_accounts():
            if exclude_id and a["id"] == exclude_id:
                continue
            try:
                p = json.loads(a.get("persona") or "{}")
                if p.get("city"):
                    used.append(p["city"])
            except Exception:
                pass
        return used

    async def add_account(self, name: str, session_key: str,
                          enable: bool = True) -> dict:
        """新增帳號（session 已驗證可用），生成地域分佈人設"""
        account_id = secrets.token_hex(6)
        persona = generate_persona(await self._used_cities())
        async with self._lifecycle_lock(account_id):
            await self.db.create_account(
                account_id, name, self.secret_box.encrypt(session_key),
                json.dumps(persona, ensure_ascii=False),
            )
            if enable:
                new_acc = await self.db.get_account(account_id)
                if new_acc:
                    await self._start_account_unlocked(new_acc)
            return await self.db.get_account(account_id)

    async def start(self, account_id: str) -> str:
        blocked = await self.live_test.start_block_error()
        if blocked:
            return blocked
        async with self._lifecycle_lock(account_id):
            blocked = await self.live_test.start_block_error()
            if blocked:
                return blocked
            acc = await self.db.get_account(account_id)
            if not acc:
                return "帳號不存在"
            if not int(acc.get("setup_complete") or 0):
                return "請先設定群組範圍，再啟動帳號"
            if not self._parse_groups(acc.get("groups")):
                await self.db.update_account(
                    account_id, enabled=0, setup_complete=0
                )
                return "請至少選擇一個有效群組，再啟動帳號"
            if account_id in self.workers:
                return "已在運行"
            await self.db.update_account(account_id, enabled=1)
            await self._refresh_managed_ids()
            acc = await self.db.get_account(account_id)
            if not acc:
                return "帳號不存在"
            await self._start_account_unlocked(acc)
            if account_id in self.workers:
                return ""
            await self.db.update_account(account_id, enabled=0)
            return "啟動失敗：session 無效或 Telegram 拒登"

    async def start_live_test_accounts(
        self,
        account_ids: list[str],
        group_id: int,
        *,
        before_release: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        """Start a validated live-test cohort while its outbound gate is locked."""
        normalized_ids = [str(account_id) for account_id in account_ids]
        async with AsyncExitStack() as stack:
            for account_id in sorted(set(normalized_ids)):
                await stack.enter_async_context(self._lifecycle_lock(account_id))
            for account_id in normalized_ids:
                acc = await self.db.get_account(account_id)
                if not acc or not int(acc.get("setup_complete") or 0):
                    return f"account {account_id} is not configured"
                if self._parse_groups(acc.get("groups")) != [int(group_id)]:
                    return "no common selected group for all four accounts"
                worker = self.workers.get(account_id)
                if worker is not None and bool(getattr(worker, "is_running", False)):
                    continue
                await self.db.update_account(account_id, enabled=1)
                await self._refresh_managed_ids()
                acc = await self.db.get_account(account_id)
                if not acc:
                    return f"account {account_id} is not configured"
                await self._start_account_unlocked(acc)
                worker = self.workers.get(account_id)
                if worker is None or not bool(getattr(worker, "is_running", False)):
                    await self.db.update_account(account_id, enabled=0)
                    return "啟動失敗：session 無效或 Telegram 拒登"
            if before_release is not None:
                await before_release()
        return ""

    async def stop(self, account_id: str) -> str:
        async with self._lifecycle_lock(account_id):
            acc = await self.db.get_account(account_id)
            if not acc:
                return "帳號不存在"
            # 先持久化停用意圖，再等待舊 worker 完整關閉。
            await self.db.update_account(account_id, enabled=0)
            worker = self.workers.pop(account_id, None)
            if worker:
                await worker.stop()
            return ""

    async def delete(self, account_id: str) -> str:
        async with self._lifecycle_lock(account_id):
            acc = await self.db.get_account(account_id)
            if not acc:
                return "帳號不存在"
            await self.db.update_account(account_id, enabled=0)
            worker = self.workers.pop(account_id, None)
            if worker:
                await worker.stop()
            await self.db.delete_account(account_id)
            return ""

    async def regen_persona(self, account_id: str) -> dict | None:
        """Regenerate only when no live-test/reconciliation state protects it."""
        async with self._lifecycle_lock(account_id):
            acc = await self.db.get_account(account_id)
            if not acc or await self._fixed_persona_mutation_blocked(account_id):
                return None
            persona = generate_persona(
                await self._used_cities(exclude_id=account_id)
            )
            if await self._fixed_persona_mutation_blocked(account_id):
                return None
            await self.db.update_account(
                account_id, persona=json.dumps(persona, ensure_ascii=False)
            )
            worker = self.workers.get(account_id)
            if worker:
                worker.persona = persona
                worker.name = persona["name"]
            return persona

    async def update_persona(self, account_id: str, persona: dict) -> dict | None:
        """Persist manual changes unless a fixed live-test persona is protected."""
        async with self._lifecycle_lock(account_id):
            acc = await self.db.get_account(account_id)
            if not acc or await self._fixed_persona_mutation_blocked(account_id):
                return None
            # Recheck immediately before the DB write so a run that became active
            # while this request waited cannot mutate its fixed persona.
            if await self._fixed_persona_mutation_blocked(account_id):
                return None
            await self.db.update_account(
                account_id, persona=json.dumps(persona, ensure_ascii=False)
            )
            worker = self.workers.get(account_id)
            if worker:
                worker.persona = persona
                worker.name = persona.get("name", worker.name)
            return persona

    async def list_available_groups(self, account_id: str) -> tuple[list[dict], str]:
        """用短暫唯讀連線列出帳號所在群組，不啟動任何互動任務。"""
        acc = await self.db.get_account(account_id)
        if not acc:
            return [], "帳號不存在"

        worker = self.workers.get(account_id)
        if worker:
            groups = worker.group_list()
            if groups:
                return groups, ""

        client = None
        try:
            client = TelegramClient(
                StringSession(self.secret_box.decrypt(acc["session_key"])),
                self.config.tg_api_id,
                self.config.tg_api_hash,
            )
            await asyncio.wait_for(client.connect(), timeout=30)
            me = await asyncio.wait_for(client.get_me(), timeout=30)
            if me is None:
                return [], "Telegram session 已失效，請刪除後重新登入"

            groups = []
            async for dialog in client.iter_dialogs():
                if not dialog.is_group:
                    continue
                title = str(getattr(dialog, "title", "") or f"群組 {dialog.id}")
                groups.append({"id": int(dialog.id), "title": title})
            groups.sort(key=lambda item: (item["title"], item["id"]))
            return groups, ""
        except asyncio.TimeoutError:
            return [], "Telegram 連線逾時，請稍後再試"
        except (OSError, RPCError, ValueError):
            return [], "Telegram 暫時無法取得群組，請稍後再試"
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except (OSError, RPCError):
                    print(f"[{account_id}] Telegram 群組探索連線關閉失敗", flush=True)

    async def save_groups(self, account_id: str, group_ids: list[int]) -> str:
        """Persist only negative peers proven to be groups for this account."""
        async with self._lifecycle_lock(account_id):
            acc = await self.db.get_account(account_id)
            if not acc:
                return "帳號不存在"
            try:
                ids = sorted({int(group_id) for group_id in group_ids})
            except (TypeError, ValueError):
                return "群組 ID 格式錯誤"
            if any(group_id >= 0 for group_id in ids):
                return "群組 ID 必須是負數，正數私訊目標禁止儲存"
            worker = self.workers.get(account_id)
            if not ids:
                if worker:
                    # 先收緊記憶體白名單，阻止已排隊工作進入發送邊界。
                    updater = getattr(worker, "update_selected_groups", None)
                    if callable(updater):
                        updater(set())
                    else:
                        worker.selected_groups = set()
                await self.db.update_account(
                    account_id,
                    groups="[]",
                    setup_complete=0,
                    enabled=0,
                )
                worker = self.workers.pop(account_id, None)
                if worker:
                    await worker.stop()
                return ""

            if worker is not None and callable(getattr(worker, "group_list", None)):
                available_groups = worker.group_list()
            else:
                available_groups, discovery_error = await self.list_available_groups(
                    account_id
                )
                if discovery_error:
                    return discovery_error
            available_ids: set[int] = set()
            for item in available_groups:
                if not isinstance(item, dict) or isinstance(item.get("id"), bool):
                    continue
                try:
                    available_id = int(item.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if available_id < 0 and item.get("is_group", True) is True:
                    available_ids.add(available_id)
            if not set(ids).issubset(available_ids):
                return "只能選擇該帳號目前實際加入的 Telegram 群組"

            await self.db.update_account(
                account_id,
                groups=json.dumps(ids, ensure_ascii=False),
                setup_complete=1,
            )
            if worker:
                updater = getattr(worker, "update_selected_groups", None)
                if callable(updater):
                    updater(set(ids))
                else:
                    worker.selected_groups = set(ids)
            return ""

    async def status(self) -> dict:
        audit_getter = getattr(self.db, "reply_event_summary", None)
        reply_audit = {}
        if callable(audit_getter):
            audit_result = audit_getter()
            reply_audit = (
                await audit_result
                if inspect.isawaitable(audit_result)
                else audit_result
            )
        accounts = []
        for acc in await self.db.list_accounts():
            worker = self.workers.get(acc["id"])
            st = self._status.get(acc["id"], {"state": "stopped", "detail": ""})
            # 指定群組（已儲存）+ 該帳號所在的群組清單（供勾選）
            sel_groups = []
            if acc.get("groups"):
                try:
                    sel_groups = json.loads(acc["groups"])
                except Exception:
                    sel_groups = []
            groups_available = worker.group_list() if worker else []
            accounts.append({
                "id": acc["id"],
                "name": acc["name"],
                "persona": acc.get("persona"),
                "groups": sel_groups,
                "groups_available": groups_available,
                "setup_complete": bool(acc.get("setup_complete")),
                "enabled": bool(acc.get("enabled")),
                "is_running": worker.is_running if worker else False,
                "state": st["state"],
                "detail": st.get("detail", ""),
                "tg_user_id": acc.get("tg_user_id"),
                "tg_username": acc.get("tg_username"),
                "stats": worker.stats if worker else {"replies_sent": 0, "errors": 0, "proactive_sent": 0},
            })
        return {
            "accounts": accounts,
            "reply_audit": reply_audit,
            "total": len(accounts),
            "running": sum(1 for a in accounts if a["is_running"]),
            "media_spend_usd": round(await self.db.media_spend_total(), 6),
            "media_budget_usd": self.config.media_daily_budget_usd,
            "features": self.feature_status(),
            "acceptance_test_mode": self.config.acceptance_test_mode,
            "water_cross_talk_probability": self.config.water_cross_talk_probability,
            "proactive_loop_seconds": [
                self.config.proactive_loop_min_seconds,
                self.config.proactive_loop_max_seconds,
            ],
        }

    async def start_live_test(self, request: dict) -> dict:
        return await self.live_test.start(request)

    async def live_test_status(self) -> dict:
        return await self.live_test.status()

    async def stop_live_test(self) -> dict:
        return await self.live_test.stop()

    async def close_all(self):
        for worker in list(self.workers.values()):
            await worker.stop()
        self.workers.clear()

    async def aclose(self):
        """關閉所有資源（worker + AI client）"""
        await self.live_test.stop("manager_close")
        await self.close_all()
        try:
            await self._media_service.aclose()
        except Exception:
            pass
        try:
            await self._ai_client.close()
        except Exception:
            pass
