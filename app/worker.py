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
import inspect
import random
import re
import time
import unicodedata

from openai import AsyncOpenAI
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.utils import get_display_name

from .persona import generate_persona, generate_proactive_topic, get_system_prompt

_MAX_REPLY_CHARS = 60

_DIRECT_VIDEO_PATTERN = re.compile(
    r"(?:視訊|视讯|視頻|视频|視屏|视屏|視像|视像|直播|實況|实况)|"
    r"(?<![a-z0-9])video\s*(?:call|chat|meet(?:ing)?)(?![a-z0-9])|"
    r"(?<![a-z0-9])live\s*stream(?:ing)?(?![a-z0-9])|"
    r"(?:開|开|start)\s*(?:個|个)?\s*(?<![a-z0-9])live(?![a-z0-9])|"
    r"(?<![a-z0-9])(?:let'?s\s+)?go\s+live(?![a-z0-9])"
)
_VIDEO_PLATFORM_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:face\s*time|skype|google\s*meet)"
    r"(?![a-z0-9])"
)
_TEAMS_INTERACTION_PATTERN = re.compile(
    r"(?:上|加入|進|进|join)\s*(?:(?<![a-z0-9])microsoft\s+)?"
    r"(?<![a-z0-9])teams(?![a-z0-9])|"
    r"(?<![a-z0-9])(?:microsoft\s+)?teams(?![a-z0-9]).{0,12}"
    r"(?:聊|通話|通话|開會|开会|視訊|视讯|call|chat|meet)"
)
_ZOOM_PATTERN = re.compile(r"(?<![a-z0-9])zoom(?![a-z0-9])")
_ZOOM_VIDEO_LEFT_PATTERN = re.compile(
    r"(?:用|使用|上|開|开|加入|透過|通过|join|call\s+on|meet\s+on)\s*$"
)
_ZOOM_VIDEO_RIGHT_PATTERN = re.compile(
    r"^\s*(?:上課|上课|聽課|听课|參加?講座|参加?讲座|講座|讲座|面試|面试|"
    r"會議|会议|開會|开会|連線|连线|聊|通話|通话|"
    r"(?<![a-z0-9])(?:call|chat|meeting|interview|class|lecture)(?![a-z0-9]))"
)
_ZOOM_SCHEDULE_RIGHT_PATTERN = re.compile(
    r"^\s*(?:in\s+(?:(?:an?|one|\d+)\s*(?:hours?|minutes?)|"
    r"(?:\d+|[一二三四五六七八九十]+)\s*(?:小時|小时|分鐘|分钟)後?)|"
    r"\d{1,2}\s*(?::\s*\d{2}|點|点))"
)
_ZOOM_TECHNICAL_LEFT_PATTERN = re.compile(
    r"(?:網頁|网页|圖片|图片|圖|图|照片|畫面|画面|地圖|地图|"
    r"optical|pinch|css|滑鼠滾輪|鼠标滚轮|鏡頭的|镜头的)\s*$"
)
_ZOOM_TECHNICAL_RIGHT_PATTERN = re.compile(
    r"^\s*(?:(?:in(?!\s+(?:(?:an?|one|\d+)\s*(?:hours?|minutes?)|"
    r"(?:\d+|[一二三四五六七八九十]+)\s*(?:小時|小时|分鐘|分钟)後?))|out)"
    r"(?![a-z0-9])|"
    r"lens|range|property|gestures?|手勢|手势|"
    r"(?:the\s+)?(?:webpage|page|image|picture|photo|map)(?![a-z0-9])|"
    r"(?:到|to)?\s*\d+%|放大|縮小|缩小|"
    r"大一點|大一点|近一點|近一点|遠一點|远一点|"
    r"一下\s*(?:這張|这张)?\s*(?:圖|图|圖片|图片|地圖|地图))"
)

_CAMERA_DEVICE_PATTERN = re.compile(
    r"(?:攝像頭|摄像头|相機|相机|"
    r"(?<![a-z0-9])(?:webcam|camera|cam)(?![a-z0-9])|"
    r"鏡頭(?!蓋)|镜头(?!盖))"
)
_CAMERA_ACTION_BEFORE_PATTERN = re.compile(
    r"(?:不要|別|别|不想|要|想|先|再|就|可以|能|麻煩|麻烦|"
    r"(?<![a-z0-9])(?:please|do\s+not|don'?t|let'?s)(?![a-z0-9]))?\s*"
    r"(?:用|使用|開|开|打開|打开|關|关|關掉|关掉|關閉|关闭|啟動|启动|"
    r"(?<![a-z0-9])(?:open|use|enable|turn\s+on|turn\s+off|switch\s+on|switch\s+off)"
    r"(?![a-z0-9]))\s*"
    r"(?:(?:一下|個|个|著|着|the|你(?:的)?|妳(?:的)?|我(?:的)?|手機|手机)\s*)*$"
)
_CAMERA_ACTION_AFTER_PATTERN = re.compile(
    r"^\s*(?:給我看|给我看|讓我看|让我看)|"
    r"^\s*(?:(?:呢|嗎|吗|吧|麻煩|麻烦|方便|可以|可不可以|能不能|"
    r"有|有沒有|有没有|記得|记得|幫我|帮我|"
    r"不要|別|别|先|再|都|給我|给我|"
    r"一下|起來|起来|is|the|[?!？！])\s*)*"
    r"(?:開|开|打開|打开|關|关|關掉|关掉|關閉|关闭|聊|通話|通话|"
    r"(?<![a-z0-9])(?:open|enable|off|call|chat)(?![a-z0-9])|"
    r"(?<![a-z0-9])on(?![a-z0-9]|\s+(?:this|that|my|the)\b))"
)
_CAMERA_SAFE_SUFFIX_PATTERN = re.compile(
    r"(?:權限|权限|設定|設置|设置|規格|规格|店|電源|电源|"
    r"電池|电池|鏡頭蓋|镜头盖|光圈|焦距|畫素|像素|開不了|开不了|"
    r"拍(?:張|张)?照|拍攝|拍摄|攝影|摄影|夜間模式|夜间模式|掃|扫|qr|條碼|条码|on\s+this\s+phone|"
    r"開箱|开箱|開賣|开卖|開機|开机|關係|关系|"
    r"settings?|specs?|repair|broken|app|on\s+sale)"
)
_PERSON_INTERACTION_PATTERN = re.compile(
    r"(?:看(?!起來|起来)|見|见|看到|見到|见到|瞧).{0,8}(?:你|妳|我)"
    r"(?!對|对|應該|应该|會|会|一定|有空|很|要|不|"
    r"的?(?:文字|訊息|消息|照片|相片))|"
    r"(?<![a-z0-9])(?:see|show)\s+(?:you|me)(?![a-z0-9])"
)
_PHOTO_OR_EQUIPMENT_PATTERN = re.compile(
    r"(?:拍照|拍攝|拍摄|攝影|摄影|照片|相片|鏡頭蓋|镜头盖|"
    r"焦距|光圈|畫素|像素|設定|設置|设置|規格|规格|相機店|相机店|"
    r"故障|維修|维修|修理|(?<![a-z0-9])(?:photo|lens|broken|repair|settings?|specs?)"
    r"(?![a-z0-9]))"
)
_REMOTE_CALL_OR_CHAT_PATTERN = re.compile(
    r"(?:聊|通話|通话)|(?<![a-z0-9])(?:call|chat)(?![a-z0-9])"
)
_CAMERA_PERSON_VIDEO_PATTERN = re.compile(
    r"(?:看|見|见|看到|見到|见到|瞧).{0,10}"
    r"(?:攝像頭|摄像头|相機|相机|鏡頭|镜头|webcam|camera|cam)"
    r".{0,10}(?:你|妳|我|臉|脸)|"
    r"(?:攝像頭|摄像头|相機|相机|鏡頭|镜头|webcam|camera|cam)"
    r".{0,8}(?:裡|里|前|中).{0,8}(?:你|妳|我|臉|脸)|"
    r"(?<![a-z0-9])(?:see|show)\s+(?:you|me|your\s+face).{0,12}"
    r"(?:on|through)\s+(?:the\s+)?(?:webcam|camera|cam)(?![a-z0-9])|"
    r"(?<![a-z0-9])(?:you|your\s+face|face).{0,12}"
    r"(?:on|through)\s+(?:the\s+)?(?:webcam|camera|cam)(?![a-z0-9])"
)
_SCREEN_PATTERN = re.compile(r"(?:螢幕|屏幕)|(?<![a-z0-9])screen(?![a-z0-9])")
_SCREEN_MEDIA_VIEW_PATTERN = re.compile(
    r"(?:看|見|见|看到|見到|见到).{0,8}(?:你|妳|我).{0,8}"
    r"(?:文字|訊息|消息|信息|內容|内容|字幕|文件|文章|照片|相片|頭像|头像|"
    r"名字|留言|貼圖|贴图|動態|动态)|"
    r"(?:螢幕|屏幕).{0,10}(?:有|有點|看到|看到|看見|在).{0,10}"
    r"(?:你|妳|我).{0,10}(?:名字|留言|訊息|消息|頭像|照片|相片|文件|文章|內容|内容)|"
    r"(?<![a-z0-9])(?:see|show).{0,8}(?:your|my).{0,5}"
    r"(?:name|comment|message|photo|avatar|sticker|document|article)(?![a-z0-9])"
)
_SCREEN_PERSON_PATTERN = re.compile(
    r"(?:螢幕|屏幕).{0,12}(?:看|見|见|看到|見到|见到).{0,6}(?:你|妳|我)|"
    r"(?:看|見|见|看到|見到|见到).{0,12}(?:螢幕|屏幕).{0,8}(?:你|妳|我)|"
    r"(?:螢幕|屏幕).{0,8}(?:上|裡|里|中).{0,8}(?:你的臉|你的脸|你本人|你本身)|"
    r"(?:螢幕|屏幕).{0,12}(?:有|裡面有|在|裡|里|中).{0,12}(?:你|妳|我)(?!的(?:名字|留言|訊息|消息|頭像|照片|相片|文件|文章|內容|内容|貼圖|贴图))|"
    r"(?:你|妳|我).{0,8}(?:出現|出现).{0,8}(?:螢幕|屏幕)|"
    r"(?:出現|出现).{0,8}(?:在).{0,8}(?:螢幕|屏幕).{0,8}(?:看|看到)?(?:你|妳|我)|"
    r"(?<![a-z0-9])(?:see|show).{0,8}(?:you|me).{0,12}(?:on|in)\s+(?:my\s+|the\s+)?screen(?![a-z0-9])|"
    r"(?:you|your\s+face|face).{0,12}(?:on|in)\s+(?:my\s+|the\s+)?screen(?![a-z0-9])|"
    r"(?<![a-z0-9])wish.{0,8}you.{0,12}(?:on|in)\s+(?:my\s+)?screen(?![a-z0-9])"
)


class AccountWorker:
    def __init__(self, account_id: str, session_key: str,
                 tg_api_id: int, tg_api_hash: str,
                 ai_client: AsyncOpenAI, db, config,
                 managed_ids: set, on_status_change,
                 persona: dict | None = None,
                 selected_groups: list[int] | None = None):
        self.account_id = account_id
        self.session_key = session_key
        self.tg_api_id = tg_api_id
        self.tg_api_hash = tg_api_hash
        self.ai_client = ai_client
        self.db = db
        self.config = config
        self.managed_ids = managed_ids  # 所有水軍 TG user id（互認）
        self.on_status_change = on_status_change
        # 指定群組：空 list = 自動所有群；非空 = 只在這幾個群活動
        self.selected_groups: set[int] = set(selected_groups or [])

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
        self._dialogs: dict[int, str] = {}  # group_id -> 群名稱（供控制台勾選）
        self._proactive_today = 0
        self._proactive_day = 0

        self.stats = {"replies_sent": 0, "errors": 0, "proactive_sent": 0}

    async def _notify_status(self, state: str, tg_user_id: int | None,
                             detail: str) -> None:
        cb = self.on_status_change
        if not cb:
            return
        result = cb(self.account_id, state, tg_user_id, detail)
        if inspect.isawaitable(result):
            await result

    # ---------- 生命周期 ----------

    async def start(self):
        try:
            await self._notify_status("connecting", None, "")
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
                        self._dialogs[d.id] = get_display_name(d) or f"群組 {d.id}"
            except Exception:
                pass

            self.is_running = True
            self._proactive_day = self._today_index()
            self._proactive_today = 0
            self._proactive_task = asyncio.create_task(self._proactive_loop())
            self._cleanup_task = asyncio.create_task(self._memory_cleanup_loop())
            await self._notify_status(
                "connected", me.id, get_display_name(me) or ""
            )
        except Exception as e:
            self.is_running = False
            self.status_detail = str(e)
            await self._notify_status("disconnected", None, str(e))

    async def stop(self):
        self.is_running = False
        await self._notify_status("stopping", None, "")
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
        await self._notify_status("stopped", None, "")

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

    def group_list(self) -> list[dict]:
        """這個帳號所在的群組（供控制台勾選指定群組）"""
        items = [
            {"id": gid, "title": title} for gid, title in self._dialogs.items()
        ]
        return items

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
            if self.selected_groups and group_id not in self.selected_groups:
                return  # 非指定群組：忽略（不回覆、不記錄）
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
            if self.selected_groups and group_id not in self.selected_groups:
                return  # 非指定群組：不歡迎新人
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
            sent = await self._send_message(
                group_id, self._welcome_text(display), short_delay=True
            )
            if not sent:
                return
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
            sent = await self._send_message(event.chat_id, text)
            if not sent:
                return
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
        reply = await self._call_ai(system_prompt, user_message)
        too_long = len(reply) > _MAX_REPLY_CHARS
        mentions_video = self._mentions_video_topic(reply)
        if not too_long and not mentions_video:
            return reply

        # 不在發送層做逐詞替換，避免改壞語意和造成 Telegram / DB 記憶不一致。
        # 長度或內容違規時共用一次重生；仍違規就不發送。
        correction = "上一版不符合要求。回覆最多 60 個字元（標點、空格也算），絕不能超過。"
        if mentions_video:
            correction += (
                "不要提及或複述禁止話題，也不要解釋拒絕原因；"
                "直接自然轉回文字聊天、交換聯絡方式或約出來見面。"
            )
        retry_message = (
            f"{user_message}\n"
            f"{correction}"
        )
        retry = await self._call_ai(system_prompt, retry_message)
        if len(retry) > _MAX_REPLY_CHARS or self._mentions_video_topic(retry):
            return ""
        return retry

    @staticmethod
    def _mentions_video_topic(text: str) -> bool:
        normalized = unicodedata.normalize("NFKC", text or "").casefold()
        normalized = re.sub(r"[‐‑‒–—−_/-]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        clauses = [
            clause.strip()
            for clause in re.split(r"[，,。；;\n]+", normalized)
            if clause.strip()
        ]

        for clause in clauses:
            if _DIRECT_VIDEO_PATTERN.search(clause):
                return True
            if _VIDEO_PLATFORM_PATTERN.search(clause):
                return True
            if _TEAMS_INTERACTION_PATTERN.search(clause):
                return True
            for zoom in _ZOOM_PATTERN.finditer(clause):
                left = clause[max(0, zoom.start() - 24):zoom.start()]
                right = clause[zoom.end():zoom.end() + 24]
                if (
                    _ZOOM_VIDEO_LEFT_PATTERN.search(left)
                    or _ZOOM_VIDEO_RIGHT_PATTERN.search(right)
                    or _ZOOM_SCHEDULE_RIGHT_PATTERN.search(right)
                ):
                    return True
                if (
                    _ZOOM_TECHNICAL_LEFT_PATTERN.search(left)
                    or _ZOOM_TECHNICAL_RIGHT_PATTERN.search(right)
                ):
                    continue
                return True

            if _CAMERA_PERSON_VIDEO_PATTERN.search(clause):
                return True
            for device in _CAMERA_DEVICE_PATTERN.finditer(clause):
                left = clause[max(0, device.start() - 36):device.start()]
                right = clause[device.end():device.end() + 36]
                # 問號／驚嘆號後通常是另一句，不能讓後句的「聊」污染
                # 前面的器材語境；但 action_after 仍保留完整 right，以辨識
                # 「你鏡頭呢？開一下吧」這種承接動作。
                local_right = re.split(r"[?!？！]", right, maxsplit=1)[0]
                remote_intent = (
                    _REMOTE_CALL_OR_CHAT_PATTERN.search(local_right)
                    or _PERSON_INTERACTION_PATTERN.search(local_right)
                )
                safe_suffix = _CAMERA_SAFE_SUFFIX_PATTERN.search(local_right)
                action_before = _CAMERA_ACTION_BEFORE_PATTERN.search(left)
                action_after = _CAMERA_ACTION_AFTER_PATTERN.search(right)
                if (action_before or action_after) and safe_suffix and not remote_intent:
                    continue
                if action_before or action_after or remote_intent:
                    return True

            if _SCREEN_PATTERN.search(clause):
                # 先移除「看你的訊息／照片／頭像」中的人稱，再檢查同句是否
                # 還有真人意圖；這樣媒體內容不誤傷，也不會掩蓋後半句看人。
                without_media_views = _SCREEN_MEDIA_VIEW_PATTERN.sub("看內容", clause)
                if _SCREEN_PERSON_PATTERN.search(without_media_views):
                    return True

        return False

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
            "請根據上下文生成自然回覆（1-3 句，台灣繁體口語；"
            "最多 60 個字元，標點、空格也算）。"
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

    async def _send_message(
        self, chat_id, text: str, short_delay: bool = False
    ) -> bool:
        if len(text) > _MAX_REPLY_CHARS:
            return False
        client = self.tg_client
        if not client or not self.is_running:
            return False
        delay = (
            random.uniform(1.0, 3.0) if short_delay
            else random.uniform(self.config.min_typing_delay, self.config.max_typing_delay)
        )
        await asyncio.sleep(delay)
        if not self.is_running or self.tg_client is not client:
            return False
        await client.send_message(chat_id, text)
        return True

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
                # 指定群組：只在勾選的群裡發言
                if self.selected_groups:
                    groups = [gid for gid in groups if gid in self.selected_groups]
                if not groups:
                    continue
                group_id = random.choice(groups)
                # 每群冷卻
                last = await self.db.last_activity(self.account_id, group_id, "proactive")
                if time.time() - last < self.config.proactive_min_interval_minutes * 60:
                    continue
                topic = generate_proactive_topic(self.persona)
                sent = await self._send_message(group_id, topic)
                if not sent:
                    continue
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
