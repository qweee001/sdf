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
    """輕量橋接：讀 DB 資料＋控制水軍帳號（啟動/停止/刪除/功能開關/新增）。"""

    def __init__(self, settings: Settings, manager: AccountManager,
                 login_service=None):
        self.settings = settings
        self.manager = manager
        self.login_service = login_service
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
            elif name in {"startacc", "stopacc", "run"}:
                body = await self._acc_toggle(name, rest)
            elif name in {"deleteacc", "del"}:
                body = await self._acc_delete(rest)
            elif name in {"media", "voice"}:
                body = await self._feature_toggle(name, rest)
            elif name == "addacc":
                body = await self._add_start(rest, event)
            elif name == "code":
                body = await self._add_code(rest, event)
            elif name == "pass":
                body = await self._add_pass(rest, event)
            elif name == "name":
                body = await self._add_name(rest, event)
            elif name == "acct":
                body = await self._acct_list()
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
            "📊 查看\n"
            "/status 運行狀態＋24h KPI\n"
            "/groups 群組清單（活動排序）\n"
            "/groups <群id> [則數] 看該群最近訊息\n"
            "/privates <帳號> [則數] 看私訊\n"
            "/acct 帳號清單\n"
            "🎛️ 控制\n"
            "/startacc <帳號> 啟動（登入/上線）\n"
            "/stopacc <帳號> 停止（下線/登出）\n"
            "/deleteacc <帳號> 刪除（含記憶）\n"
            "/media on|off 媒體功能開關\n"
            "/voice on|off 語音功能開關\n"
            "➕ 新增帳號（TG 驗證碼流程）\n"
            "/addacc +886…5678 傳驗證碼\n"
            "/code 12345 輸入驗證碼\n"
            "/pass xxx 兩步驗證密碼（若需要）\n"
            "/name 台北-美玲 建立帳號（暫不啟動）\n"
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

    # ---------- 帳號控制 ----------

    def _find_account_sync(self, accounts: list[dict], target: str) -> dict | None:
        return next(
            (
                a
                for a in accounts
                if str(a.get("id")) == target or str(a.get("name") or "") == target
            ),
            None,
        )

    async def _acct_list(self) -> str:
        accounts = await self.manager.db.list_accounts()
        if not accounts:
            return "還沒有水軍帳號"
        workers = getattr(self.manager, "workers", {}) or {}
        lines = ["水軍帳號", "──────────────"]
        for a in accounts:
            persona = {}
            if isinstance(a.get("persona"), str):
                try:
                    import json

                    persona = json.loads(a["persona"])
                except Exception:
                    persona = {}
            worker = workers.get(str(a.get("id")))
            running = bool(worker.is_running) if worker else False
            st = "🟢" if running else "⚪"
            on = "啟用" if a.get("enabled") else "停用"
            lines.append(
                f"{st} {a.get('name')}（{persona.get('name', '')}）[{on}]"
            )
        lines.append("")
        lines.append("控制：/startacc、/stopacc、/deleteacc <帳號>")
        return "\n".join(lines)

    async def _acc_toggle(self, name: str, args: list[str]) -> str:
        if not args:
            return f"用法：/{name} <帳號名或id>"
        target = args[0]
        accounts = await self.manager.db.list_accounts()
        acc = self._find_account_sync(accounts, target)
        if acc is None:
            names = "、".join(str(a.get("name")) for a in accounts)
            return f"找不到帳號「{target}」；現有：{names}"
        if name in {"startacc", "run"}:
            err = await self.manager.start(acc["id"])
            verb = "啟動"
        else:
            err = await self.manager.stop(acc["id"])
            verb = "停止"
        if err:
            return f"⚠️ {verb}失敗：{err}"
        mark = "🟢" if verb == "啟動" else "⚪"
        return f"{mark} {acc.get('name')} 已{verb}"

    async def _acc_delete(self, args: list[str]) -> str:
        if not args:
            return "用法：/deleteacc <帳號名或id>"
        target = args[0]
        accounts = await self.manager.db.list_accounts()
        acc = self._find_account_sync(accounts, target)
        if acc is None:
            names = "、".join(str(a.get("name")) for a in accounts)
            return f"找不到帳號「{target}」；現有：{names}"
        err = await self.manager.delete(acc["id"])
        if err:
            return f"⚠️ 刪除失敗：{err}"
        return f"🗑️ 已刪除 {acc.get('name')}（含記憶）"

    async def _feature_toggle(self, name: str, args: list[str]) -> str:
        on = (args[0].lower() if args else "") in {"on", "1", "true", "開", "開啟"}
        media = name == "media"
        voice = name == "voice"
        # 讀目前狀態：manager 只從 config 拿，這裡用現有開關的取反＋目標
        current = self.manager.feature_status()
        media_enabled = on if media else current["media_enabled"]
        voice_enabled = on if voice else current["voice_enabled"]
        err = await self.manager.update_feature_flags(
            media_enabled=media_enabled, voice_enabled=voice_enabled
        )
        if err:
            return f"⚠️ {err}"
        st = self.manager.feature_status()
        return (
            f"媒體：{'開啟' if st['media_enabled'] else '關閉'}｜"
            f"語音：{'開啟' if st['voice_enabled'] else '關閉'}"
        )

    # ---------- 新增帳號（TG 驗證碼流程） ----------

    async def _add_start(self, args: list[str], event) -> str:
        if self.login_service is None:
            return "⚠️ 登入服務未初始化"
        if not args:
            return "用法：/addacc +886****5678"
        try:
            r = await self.login_service.start(args[0])
        except Exception as e:
            return f"⚠️ {e}"
        event.chat_id  # 確保有 chat 上下文
        return (
            f"驗證碼已傳送至 {r.get('phone_hint')}（5 分鐘內有效）\n"
            f"收到後回：/code 12345"
        )

    async def _add_code(self, args: list[str], event) -> str:
        if self.login_service is None:
            return "⚠️ 登入服務未初始化"
        if not args:
            return "用法：/code 12345"
        auth_id = await self._last_auth_id()
        if not auth_id:
            return "⚠️ 先 /addacc 發驗證碼"
        try:
            r = await self.login_service.submit_code(auth_id, args[0])
        except Exception as e:
            return f"⚠️ {e}"
        if r.get("status") == "password_required":
            return "需要兩步驗證密碼：/pass xxx"
        if r.get("status") == "authorized":
            return "✅ 驗證成功！回：/name 台北-美玲（帳號名稱）"
        return f"狀態：{r.get('status')}"

    async def _add_pass(self, args: list[str], event) -> str:
        if self.login_service is None:
            return "⚠️ 登入服務未初始化"
        if not args:
            return "用法：/pass xxx"
        auth_id = await self._last_auth_id()
        if not auth_id:
            return "⚠️ 流程不存在或已過期"
        try:
            r = await self.login_service.submit_password(auth_id, args[0])
        except Exception as e:
            return f"⚠️ {e}"
        if r.get("status") == "authorized":
            return "✅ 驗證成功！回：/name 台北-美玲（帳號名稱）"
        return f"狀態：{r.get('status')}"

    async def _add_name(self, args: list[str], event) -> str:
        if self.login_service is None:
            return "⚠️ 登入服務未初始化"
        auth_id = await self._last_auth_id()
        if not auth_id:
            return "⚠️ 先完成 /addacc → /code 流程"
        name = args[0] if args else "水軍帳號"
        try:
            verified = await self.login_service.claim(auth_id)
        except Exception as e:
            return f"⚠️ {e}"
        account = await self.manager.add_account(
            name, verified.session_string, enable=False,
            display_name=str(getattr(verified, "tg_name", "") or ""),
        )
        await self.manager.db.update_account(
            account["id"],
            tg_user_id=verified.tg_user_id,
            tg_username=str(getattr(verified, "tg_name", "") or ""),
            avatar=str(getattr(verified, "avatar", "") or ""),
            enabled=0,
        )
        return (
            f"✅ 已建立 {account['name']}（{verified.tg_name}），暫不啟動。\n"
            f"先設人設與群組，再 /startacc {account['name']} 上線。"
        )

    async def _last_auth_id(self) -> str:
        """新增流程是單使用者（管理者）串行的，用最近一次 start 的 auth_id。"""
        pending = getattr(self.login_service, "pending", {}) or {}
        if not pending:
            return ""
        latest = max(pending.values(), key=lambda p: p.created_at)
        return latest.auth_id


def make_bot(settings: Settings, manager: AccountManager,
             login_service=None) -> TgControlBot | None:
    if settings.bot_token:
        return TgControlBot(settings, manager, login_service)
    return None
