"""
帳號工作線程 - 管理單個水軍帳號的 Telegram 聊天邏輯
無安全審核、無成人 尺度限制。

行為模型（水軍）：
1. 背景活躍：主動發言維持群生命感
2. 真人必接：@我/回覆我 → 必回（5-20 秒人味延遲）；其他真人訊息按概率
3. 新人攬客：偵測入群 → 自然歡迎（30% 觸發，避免齊聲）
4. 曬成約：低頻社會證明（人設層控制）
5. 漸進升級：先調情後直球（人設層控制）
6. 作息錯峰：每人隨機偏移，凌晨全群睡覺
7. 水軍互認：managed_ids 互相識別（不硬聊，15% 概率製造群自己會活）
"""

from __future__ import annotations

import asyncio
import random
import time

from openai import AsyncOpenAI
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.utils import get_display_name

from .persona import generate_persona, generate_proactive_topic, get_system_prompt


class AccountWorker:
    def __init__(self, account_id: str, session_key: str,
                 tg_api_id: int, tg_api_hash: str,
                 ai_client: AsyncOpenAI, db, config,
                 managed_ids: set, on_status_change,
                 persona: dict | None = None):
        self.account_id = account_id
        self.session_key = session_key
        self.tg_api_id = tg_api_id
        self.tg_api_hash = tg_api_hash
        self.ai_client = ai_client
        self.db = db
        self.config = config
        self.managed_ids = managed_ids  # 所有水軍 TG user id（互認）
        self.on_status_change = on_status_change

        self.persona = persona or generate_persona()
        self.name = self.persona["name"]
        # 作息錯峰：每人隨機偏移 0-24 小時，避免同時醒睡
        self._schedule_offset = random.uniform(0, 24)

        self.tg_client: TelegramClient | None = None
        self.tg_user_id: int | None = None
        self.is_running = False
        self.status_detail = ""

        self._proactive_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._last_activity: dict[int, float] = {}  # group_id -> ts
        self._known_groups: set[int] = set()  # 這個帳號知道的所有群（冷啟動 fallback）
        self._proactive_today = 0
        self._proactive_day = 0

        self.stats = {"replies_sent": 0, "errors": 0, "proactive_sent": 0}

    # ---------- 生命周期 ----------

    async def start(self):
        try:
            session = StringSession(self.session_key)
            self.tg_client = TelegramClient(session, self.tg_api_id, self.tg_api_hash)
            await asyncio.wait_for(self.tg_client.connect(), timeout=30)
            me = await self.tg_client.get_me()
            if me is None:
                raise ValueError("無法獲取帳號資訊")
            self.tg_user_id = int(me.id)
            self.managed_ids.add(self.tg_user_id)

            self.tg_client.add_event_handler(self.on_message, events.NewMessage())
            self.tg_client.add_event_handler(self.on_chat_action, events.ChatAction())

            # 冷啟動：先把這個帳號在的所有群記下來（就算沒人講話，群也不會死）
            try:
                async for d in self.tg_client.iter_dialogs():
                    if d.is_group:
                        self._known_groups.add(d.id)
            except Exception:
                pass

            self.is_running = True
            self._proactive_day = self._today_index()
            self._proactive_today = 0
            self._proactive_task = asyncio.create_task(self._proactive_loop())
            self._cleanup_task = asyncio.create_task(self._memory_cleanup_loop())
            self.on_status_change(self.account_id, "connected", me.id,
                                  get_display_name(me) or "")
        except Exception as e:
            self.is_running = False
            self.status_detail = str(e)
            self.on_status_change(self.account_id, "disconnected", None, str(e))

    async def stop(self):
        self.is_running = False
        for task in (self._proactive_task, self._cleanup_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._proactive_task = None
        self._cleanup_task = None
        if self.tg_client:
            await self.tg_client.disconnect()
            self.tg_client = None
        self.on_status_change(self.account_id, "stopped", None, "")

    # ---------- 作息 ----------

    @staticmethod
    def _today_index() -> int:
        return int(time.time() // 86400)

    def _is_sleeping(self) -> bool:
        """凌晨 4-7 點睡覺（每人錯峰偏移）"""
        h = (time.time() / 3600 + self._schedule_offset) % 24
        return (h + 20) % 24 < 3

    def _is_busy_hour(self) -> bool:
        """工作時間 9-17 點，主動發言降頻"""
        h = (time.time() / 3600 + self._schedule_offset) % 24
        return 9 <= h < 17

    # ---------- 事件處理 ----------

    async def on_message(self, event):
        if not self.is_running or not self.tg_client:
            return
        try:
            if event.is_private:
                await self._handle_private(event)
                return
            if not event.is_group or event.chat_id is None:
                return
            group_id = int(event.chat_id)
            self._known_groups.add(group_id)
            self._last_activity[group_id] = time.time()
            await self.db.add_message(
                self.account_id, group_id,
                int(event.sender_id or 0),
                get_display_name(await event.get_sender()) or "",
                "user", event.raw_text or "",
            )
            if not await self._should_reply(event):
                return
            # 人味延遲：被@/回覆 → 5-20 秒；普通 → 8-45 秒
            is_hot = event.mentioned or (event.is_reply and event.reply_to)
            delay = random.uniform(5, 20) if is_hot else random.uniform(8, 45)
            asyncio.create_task(self._reply_later(event, delay))
        except Exception as e:
            self.stats["errors"] += 1
            print(f"[{self.name}] on_message error: {e}", flush=True)

    async def on_chat_action(self, event):
        """新人入群 → 自然歡迎（攬客）"""
        if not event.user_joined:
            return
        if not self.is_running or not self.tg_client:
            return
        try:
            if not event.is_group or event.chat_id is None:
                return
            group_id = int(event.chat_id)
            self._known_groups.add(group_id)
            new_user = await event.get_user()
            if new_user is None:
                return
            uid = int(getattr(new_user, "id", 0) or 0)
            if uid in self.managed_ids:
                return  # 水軍進群不用歡迎
            # 30% 觸發（避免水軍齊聲歡迎）
            if random.random() > 0.3:
                return
            await asyncio.sleep(random.uniform(5, 15))
            if not self.is_running:
                return
            display = get_display_name(new_user) or "新朋友"
            await self._send_message(group_id, self._welcome_text(display), short_delay=True)
            self.stats["proactive_sent"] += 1
            await self.db.touch_activity(self.account_id, group_id, "proactive")
        except Exception as e:
            print(f"[{self.name}] chat_action error: {e}", flush=True)

    def _welcome_text(self, name: str) -> str:
        gender = self.persona["gender"]
        city = self.persona["city"]
        if gender == "女":
            templates = [
                f"歡迎歡迎～ {name} 是哪裡的呀？",
                f"新來的 {name}！好開心有人加入 XD",
                f"嗨 {name}～ 剛加入嗎？我們這邊人都不會咬人的啦",
                f"{name} 你好呀～ 我們住{city}的比較多，你在哪邊？",
            ]
        else:
            templates = [
                f"嗨 {name}，歡迎加入～ 哪裡的兄弟？",
                f"{name} 好，歡迎！我們這邊都很正常不會亂來",
                f"歡迎新會員 {name} XD 有問題問我們就好",
                f"嗨 {name}～ 我們都是認真想認識人的，放心",
            ]
        return random.choice(templates)

    async def _handle_private(self, event):
        """私訊只記錄不回覆（防炸號），真人走助理流程"""
        try:
            sender_id = int(event.sender_id or 0)
            if sender_id in self.managed_ids:
                return
            sender = await event.get_sender()
            sender_name = get_display_name(sender) or f"用戶{sender_id}"
            preview = (event.raw_text or "")[:200]
            await self.db.add_private_message(
                self.account_id, sender_id, sender_name, preview
            )
        except Exception:
            pass

    # ---------- 回覆決策 ----------

    async def _should_reply(self, event) -> bool:
        sender_id = int(event.sender_id or 0)
        if sender_id == self.tg_user_id:
            return False
        # 被@或回覆 → 必回（真人必接）
        if event.mentioned or (event.is_reply and event.reply_to):
            return True
        # 水軍對水軍：15% 概率（群自己會活，但不出破綻）
        if sender_id in self.managed_ids:
            return random.random() < self.config.water_cross_talk_probability
        # 真人 → 按概率（活躍群降頻）
        group_id = int(event.chat_id or 0)
        p = self.config.base_reply_probability
        if group_id in self._last_activity:
            since = time.time() - self._last_activity[group_id]
            if since < 60:
                p *= 0.4
            elif since < 300:
                p *= 0.7
        return random.random() < p

    async def _reply_later(self, event, delay: float):
        await asyncio.sleep(delay)
        if not self.is_running or not self.tg_client:
            return
        try:
            text = await self._generate_reply(event)
            if not text:
                return
            await self._send_message(event.chat_id, text)
            self.stats["replies_sent"] += 1
            await self.db.add_message(
                self.account_id, int(event.chat_id), self.tg_user_id or 0,
                self.name, "assistant", text,
            )
            await self.db.touch_activity(self.account_id, int(event.chat_id), "reply")
        except Exception as e:
            self.stats["errors"] += 1
            print(f"[{self.name}] reply error: {e}", flush=True)

    async def _generate_reply(self, event) -> str:
        group_id = int(event.chat_id or 0)
        history = await self.db.get_recent_messages(
            self.account_id, group_id, self.config.memory_max_messages
        )
        system_prompt = get_system_prompt(self.persona)
        user_message = self._build_user_message(event, history)
        return await self._call_ai(system_prompt, user_message)

    def _build_user_message(self, event, history: list[dict]) -> str:
        recent = history[-10:] if history else []
        context = ""
        if recent:
            context = "最近對話：\n"
            for msg in recent:
                role = "我" if msg["role"] == "assistant" else msg["sender_name"]
                context += f"[{role}] {msg['content']}\n"
        sender_name = ""
        try:
            sender_name = get_display_name(event.sender) or ""
        except Exception:
            pass
        is_water = (int(event.sender_id or 0) in self.managed_ids)
        water_hint = "（對方是群組裡另一位付費會員）" if is_water else ""
        return (
            f"{context}"
            f"最新消息：[{sender_name or '有人'}]{water_hint} {event.raw_text}\n"
            "請根據上下文生成自然回覆（1-3 句，台灣繁體口語）。"
        )

    async def _call_ai(self, system_prompt: str, user_message: str) -> str:
        if not self.config.ai_model:
            return ""
        for attempt in range(2):
            try:
                resp = await self.ai_client.chat.completions.create(
                    model=self.config.ai_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=self.config.ai_temperature,
                    max_tokens=self.config.ai_max_tokens,
                    timeout=self.config.ai_timeout,
                )
                content = resp.choices[0].message.content
                if content:
                    return content.strip()
                return ""
            except FloodWaitError:
                return ""
            except Exception as e:
                print(f"[{self.name}] AI error: {e}", flush=True)
                if attempt == 0:
                    await asyncio.sleep(2)
        return ""

    # ---------- 發送 ----------

    async def _send_message(self, chat_id, text: str, short_delay: bool = False):
        if not self.tg_client:
            return
        delay = (
            random.uniform(1.0, 3.0) if short_delay
            else random.uniform(self.config.min_typing_delay, self.config.max_typing_delay)
        )
        await asyncio.sleep(delay)
        max_len = 4096
        if len(text) > max_len:
            for i in range(0, len(text), max_len):
                await self.tg_client.send_message(chat_id, text[i:i + max_len])
        else:
            await self.tg_client.send_message(chat_id, text)

    # ---------- 主動發言 ----------

    async def _proactive_loop(self):
        while self.is_running:
            try:
                # 隨機間隔 4-12 分鐘（錯峰）
                await asyncio.sleep(random.uniform(240, 720))
                if not self.is_running:
                    return
                if self.config.proactive_enabled and self._is_sleeping():
                    continue
                if self._proactive_today >= self.config.proactive_max_per_day:
                    continue
                if self._is_busy_hour() and random.random() < 0.5:
                    continue
                # 挑一個最近活躍的群組（6 小時內）；都沒有的話用已知群 fallback（群不能死）
                groups = [
                    gid for gid, ts in self._last_activity.items()
                    if time.time() - ts < 6 * 3600
                ]
                if not groups:
                    groups = list(self._known_groups)
                if not groups:
                    continue
                group_id = random.choice(groups)
                # 每群冷卻
                last = await self.db.last_activity(self.account_id, group_id, "proactive")
                if time.time() - last < self.config.proactive_min_interval_minutes * 60:
                    continue
                topic = generate_proactive_topic(self.persona)
                await self._send_message(group_id, topic)
                self._proactive_today += 1
                self.stats["proactive_sent"] += 1
                await self.db.add_message(
                    self.account_id, group_id, self.tg_user_id or 0,
                    self.name, "assistant", topic,
                )
                await self.db.touch_activity(self.account_id, group_id, "proactive")
                # 每日重置
                today = self._today_index()
                if today != self._proactive_day:
                    self._proactive_day = today
                    self._proactive_today = 0
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[{self.name}] proactive error: {e}", flush=True)
                await asyncio.sleep(60)

    async def _memory_cleanup_loop(self):
        while self.is_running:
            await asyncio.sleep(3600)
            try:
                await self.db.cleanup_expired(self.config.memory_ttl_hours)
            except Exception:
                pass
