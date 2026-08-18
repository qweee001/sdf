"""
帳號管理器 - 管理所有水軍帳號的生命週期
"""

from __future__ import annotations

import asyncio
import json
import secrets

from openai import AsyncOpenAI

from .config import Settings
from .crypto import SecretBox
from .database import Database
from .persona import generate_persona
from .worker import AccountWorker


class AccountManager:
    def __init__(self, config: Settings, db: Database, secret_box: SecretBox):
        self.config = config
        self.db = db
        self.secret_box = secret_box
        self.workers: dict[str, AccountWorker] = {}
        self.managed_ids: set[int] = set()
        self._status: dict[str, dict] = {}
        self._ai_client = AsyncOpenAI(
            base_url=config.ai_base_url,
            api_key=config.ai_api_key or "sk-placeholder",
            timeout=config.ai_timeout,
        )

    async def _on_status_change(self, account_id: str, state: str,
                                tg_user_id: int | None, detail: str):
        self._status[account_id] = {"state": state, "detail": detail}

    async def start_all(self):
        """部署重啟後恢復所有 enabled 帳號"""
        for acc in await self.db.list_accounts():
            if acc.get("enabled"):
                await self._start_account(acc)

    async def _start_account(self, acc: dict):
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
        selected_groups = []
        if acc.get("groups"):
            try:
                raw = json.loads(acc["groups"])
                if isinstance(raw, list):
                    selected_groups = [int(g) for g in raw]
            except Exception:
                selected_groups = []
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
        await self.db.create_account(
            account_id, name, self.secret_box.encrypt(session_key),
            json.dumps(persona, ensure_ascii=False),
        )
        if enable:
            new_acc = await self.db.get_account(account_id)
            if new_acc:
                await self._start_account(new_acc)
        return await self.db.get_account(account_id)

    async def start(self, account_id: str) -> str:
        acc = await self.db.get_account(account_id)
        if not acc:
            return "帳號不存在"
        if account_id in self.workers:
            return "已在運行"
        await self.db.update_account(account_id, enabled=1)
        await self._start_account(acc)
        return "" if account_id in self.workers else "啟動失敗：session 無效或 Telegram 拒登"

    async def stop(self, account_id: str) -> str:
        worker = self.workers.pop(account_id, None)
        if worker:
            await worker.stop()
        await self.db.update_account(account_id, enabled=0)
        return ""

    async def delete(self, account_id: str) -> str:
        worker = self.workers.pop(account_id, None)
        if worker:
            await worker.stop()
        await self.db.delete_account(account_id)
        return ""

    async def regen_persona(self, account_id: str) -> dict | None:
        """重新生成人設（換一個城市的真人）"""
        acc = await self.db.get_account(account_id)
        if not acc:
            return None
        persona = generate_persona(await self._used_cities(exclude_id=account_id))
        await self.db.update_account(
            account_id, persona=json.dumps(persona, ensure_ascii=False)
        )
        worker = self.workers.get(account_id)
        if worker:
            worker.persona = persona
            worker.name = persona["name"]
        return persona

    async def update_persona(self, account_id: str, persona: dict) -> dict | None:
        """手動修改人設（含性格），儲存並套用至運行中的 worker"""
        acc = await self.db.get_account(account_id)
        if not acc:
            return None
        await self.db.update_account(
            account_id, persona=json.dumps(persona, ensure_ascii=False)
        )
        worker = self.workers.get(account_id)
        if worker:
            worker.persona = persona
            worker.name = persona.get("name", worker.name)
        return persona

    async def save_groups(self, account_id: str, group_ids: list[int]) -> str:
        """指定群組：儲存至 DB，並套用至運行中的 worker（熱更新）"""
        acc = await self.db.get_account(account_id)
        if not acc:
            return "帳號不存在"
        ids = sorted({int(g) for g in group_ids})
        await self.db.update_account(
            account_id, groups=json.dumps(ids, ensure_ascii=False)
        )
        worker = self.workers.get(account_id)
        if worker:
            worker.selected_groups = set(ids)
        return ""

    async def status(self) -> dict:
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
            "total": len(accounts),
            "running": sum(1 for a in accounts if a["is_running"]),
        }

    async def close_all(self):
        for worker in list(self.workers.values()):
            await worker.stop()
        self.workers.clear()

    async def aclose(self):
        """關閉所有資源（worker + AI client）"""
        await self.close_all()
        try:
            await self._ai_client.close()
        except Exception:
            pass
