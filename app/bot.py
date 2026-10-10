"""
Telegram Bot 橋接 - 把控制台常用功能接進 Telegram Bot（遠端查看）。

用 BotFather 的 bot token（BOT_TOKEN 環境變數），長輪詢 getUpdates；
bot 和水軍帳號完全獨立（bot 不是 user account，不會進群、不占 session）。

指令：
  /status              運行狀態 + 24h KPI
  /groups              群組清單（依活動排序，前 8 個）
  /groups <id> [n]     某群最近 n 則訊息（預設 20）
  /privates <帳號> [n] 某水軍帳號收到的私訊（預設 15）
  /start /help         說明

權限：BOT_ADMIN_IDS（逗號分隔的 TG user id）。留空＝全部放行（會打警告）。
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re

from telethon import TelegramClient, events

from .config import Settings
from .manager import AccountManager

log = logging.getLogger("sdf.bot")

MSG_LIMIT = 4096


def _fmt_ts(ts: float) -> str:
    try:
        dt = datetime.datetime.fromtimestamp(float(ts), datetime.UTC).astimezone(
            datetime.timezone(datetime.timedelta(hours=8))
        )
    except Exception:
        dt = datetime.datetime.fromtimestamp(float(ts))
    return dt.strftime("%m-%d %H:%M")


class TgControlBot:
    """一個輕量橋接：讀 DB 的既有資料，發回 Telegram 訊息，不改狀態。"""

    def __init__(self, settings: Settings, manager: AccountManager):
        self.settings = settings
        self.manager = manager
        self.bot_token = settings.bot_token
        self.admin_ids = tuple(str(x) for x in settings.bot_admin_ids if str(x))
        self._client: TelegramClient | None = None
        self._bot_username = ""
        if not self.admin_ids:
            log.warning("BOT_ADMIN_IDS 為空：任何 TG 使用者都能操作 bot")

    # ---------- 生命週期 ----------

    async def run(self) -> None:
        client = TelegramClient(
            "sdf_control_bot", self.settings.tg_api_id, self.settings.tg_api_hash
        )
        self._client = client
        await client.start(bot_token=self.bot_token)
        me = await client.get_me()
        self._bot_username = str(getattr(me, "username", "") or "")
        client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        log.info("Telegram 控制台 bot 上線（@%s）", self._bot_username)
        try:
            await client.run_until_disconnected()
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    # ---------- 權限 ----------

    def _allowed(self, event) -> bool:
        if not self.admin_ids:
            return True
        sender = event.sender
        if sender is None:
            return False
        return str(getattr(sender, "id", "")) in self.admin_ids

    # ---------- 指令處理 ----------

    async def _on_message(self, event) -> None:
        if not self._allowed(event):
            await event.reply("⚠️ 不是管理者（BOT_ADMIN_IDS 未包含你）")
            return
        text = (event.raw_text or "").strip()
        cmd = text.split()
        name = cmd[0].lower().lstrip("/").split("@")[0] if cmd else ""
        rest: list[str] = list(cmd[1:])
        try:
            if name in {"start", "help", ""}:
                body = self._help()
            elif name == "status":
                body = await self._status()
            elif name == "groups":
                body = await self._groups(rest)
            elif name in {"privates", "priv"}:
                body = await self._privates(rest)
            else:
                body = f"不認識的指令「/{name}」\n" + self._help()
            await event.reply(body[:MSG_LIMIT])
        except Exception as e:
            log.exception("bot 指令 %r 失敗", text)
            await event.reply(f"⚠️ {type(e).__name__}: {e}")

    # ---------- 各指令內容 ----------

    def _help(self) -> str:
        return (
            "SDF 控制台 bot\n"
            "──────────────\n"
            "/status 運行狀態＋24h KPI\n"
            "/groups 群組清單（活動排序）\n"
            "/groups <群id> [則數] 看該群最近訊息（預設 20）\n"
            "/privates <帳號> [則數] 看該水軍的私訊（預設 15）\n"
            "/start 本說明"
        )

    async def _status(self) -> str:
        data = await self.manager.status()
        lines = ["SDF 控制台", "──────────────"]
        running = data.get("running", 0)
        total = data.get("total", 0)
        audit = data.get("reply_audit", {}) or {}
        sent = int(((audit.get("sent") or {}).get("ok") or 0))
        policy = audit.get("policy") or {}
        blocked = sum(int(v) or 0 for v in policy.values())
        top_reason, top_n = "", 0
        for r, n in policy.items():
            if int(n or 0) > top_n:
                top_reason, top_n = r, int(n)
        held = sum(
            int((a.get("stats") or {}).get("gate_held") or 0)
            for a in data.get("accounts", [])
        )
        lines.append(f"運行 {running}/{total}｜待處置 {held}")
        line = f"24h 送出 {sent}｜攔截 {blocked}"
        if top_reason:
            line += f"（最多：{top_reason} {top_n}）"
        lines.append(line)
        for acc in data.get("accounts", []):
            persona = {}
            raw = acc.get("persona")
            if isinstance(raw, str):
                try:
                    import json

                    persona = json.loads(raw)
                except Exception:
                    persona = {}
            st = acc.get("stats") or {}
            state = "🟢" if acc.get("is_running") else "⚪"
            lines.append(
                f"{state} {acc.get('name')}（{persona.get('name', '')}）"
                f"｜回覆 {st.get('replies_sent', 0)}"
                f"｜主動 {st.get('proactive_sent', 0)}"
            )
        return "\n".join(lines)

    async def _groups(self, args: list[str]) -> str:
        # 有群 id 參數：看該群訊息
        if args:
            try:
                gid = int(args[0])
            except ValueError:
                return f"群 id 要數字，收到「{args[0]}」"
            try:
                n = max(1, min(60, int(args[1])))
            except (ValueError, IndexError):
                n = 20
            rows = await self.manager.db.get_group_messages(gid, n)
            rows = list(reversed(rows))  # 舊→新
            lines = [f"群 {gid} 最近 {len(rows)} 則", "──────────────"]
            for row in rows:
                role = "水軍" if str(row.get("role")) == "assistant" else "真人"
                who = str(row.get("sender_name") or "?")[:10]
                ts = _fmt_ts(row.get("timestamp") or 0)
                content = str(row.get("content") or "")[:80]
                lines.append(f"[{ts}] {role}·{who}：{content}")
            return "\n".join(lines)

        # 無參數：清單（活動排序前 8）
        db = self.manager.db
        overview_rows = await db.group_overview(exclude_senders=())
        rows = sorted(
            overview_rows,
            key=lambda r: (-float(r.get("last_ts") or 0), r.get("group_id")),
        )[:8]
        if not rows:
            return "還沒有群組紀錄（先讓水軍上線收訊息）"
        labels = await db.get_group_labels()
        lines = ["群組清單（按最近活動）", "──────────────"]
        for r in rows:
            gid = r["group_id"]
            label = labels.get(gid, "")
            name = label or f"群組 {gid}"
            lines.append(
                f"▸ {name}（{gid}）\n"
                f"  訊息 {int(r.get('msg_count') or 0)}"
                f"｜真人 {int(r.get('human_senders') or 0)}"
                f"｜最近 {_fmt_ts(float(r.get('last_ts') or 0))}"
            )
        lines.append("")
        lines.append("看訊息：/groups <群id> [則數]")
        return "\n".join(lines)

    async def _privates(self, args: list[str]) -> str:
        if not args:
            return "用法：/privates <帳號名或id> [則數]"
        target = args[0]
        try:
            n = max(1, min(40, int(args[1])))
        except (ValueError, IndexError):
            n = 15
        accounts = await self.manager.db.list_accounts()
        acc = next(
            (
                a
                for a in accounts
                if str(a.get("id")) == target or str(a.get("name") or "") == target
            ),
            None,
        )
        if acc is None:
            names = "、".join(str(a.get("name")) for a in accounts)
            return f"找不到帳號「{target}」；現有：{names}"
        msgs = await self.manager.db.get_private_messages(acc["id"], limit=n)
        if not msgs:
            return f"{acc.get('name')}：{n} 則內沒有私訊"
        lines = [f"{acc.get('name')} 收到的私訊", "──────────────"]
        for m in msgs:
            ts = _fmt_ts(m.get("timestamp") or 0)
            who = str(m.get("sender_name") or m.get("sender_id") or "?")[:10]
            mark = "🔵未讀 " if not m.get("read") else "     "
            lines.append(f"{mark}[{ts}] {who}：{str(m.get('content') or '')[:70]}")
        return "\n".join(lines)


def make_bot(settings: Settings, manager: AccountManager) -> TgControlBot | None:
    if settings.bot_token:
        return TgControlBot(settings, manager)
    return None
