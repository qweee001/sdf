from __future__ import annotations

import asyncio
import logging
import random
import signal
import time
from collections import defaultdict
from datetime import date

from openai import AsyncOpenAI
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message
from telethon.utils import get_display_name

from .config import Settings, load_settings
from .memory import MemoryStore
from .prompts import proactive_prompt, response_prompt, system_prompt


LOGGER = logging.getLogger("telegram-ai-userbot")


class UserbotApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = TelegramClient(
            StringSession(settings.tg_session_string),
            settings.tg_api_id,
            settings.tg_api_hash,
        )
        self.ai = AsyncOpenAI(
            api_key=settings.ai_api_key,
            base_url=settings.ai_base_url,
            timeout=45,
            max_retries=2,
        )
        self.memory = MemoryStore(settings.memory_db_path, settings.memory_ttl_hours)
        self.me_id = 0
        self.me_name = "這個帳號"
        self.last_activity: dict[int, float] = {}
        self.last_proactive: dict[int, float] = {}
        self.next_proactive_interval: dict[int, float] = {}
        self.proactive_counts: dict[tuple[int, date], int] = defaultdict(int)
        self.group_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.background_tasks: list[asyncio.Task[None]] = []

    def group_allowed(self, group_id: int) -> bool:
        allowed = self.settings.group_chat_ids
        return not allowed or group_id in allowed

    async def generate(self, user_prompt: str) -> str:
        result = await self.ai.chat.completions.create(
            model=self.settings.ai_model,
            messages=[
                {"role": "system", "content": system_prompt(self.settings)},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = result.choices[0].message.content
        if not content or not content.strip():
            raise RuntimeError("AI provider returned an empty reply")
        return content.strip()[:4000]

    async def was_reply_to_me(self, message: Message) -> bool:
        if not message.is_reply:
            return False
        replied = await message.get_reply_message()
        return bool(replied and replied.sender_id == self.me_id)

    async def should_reply(self, event: events.NewMessage.Event) -> bool:
        message = event.message
        if self.settings.reply_on_mention and bool(message.mentioned):
            return True
        if self.settings.reply_on_reply and await self.was_reply_to_me(message):
            return True
        return random.random() <= self.settings.group_reply_probability

    async def on_message(self, event: events.NewMessage.Event) -> None:
        if not event.is_group or event.chat_id is None:
            return

        group_id = int(event.chat_id)
        if not self.group_allowed(group_id):
            return

        text = (event.raw_text or "").strip()
        if not text:
            return

        sender = await event.get_sender()
        sender_id = int(event.sender_id or 0)
        if sender_id in self.settings.ignore_sender_ids:
            return
        sender_name = get_display_name(sender) if sender is not None else f"成員 {sender_id}"
        now = time.time()
        self.last_activity[group_id] = now

        await self.memory.add(group_id, sender_id, sender_name, "user", text)
        if not await self.should_reply(event):
            return

        async with self.group_locks[group_id]:
            history = await self.memory.recent_group(
                group_id,
                self.settings.memory_history_limit,
            )
            try:
                async with self.client.action(group_id, "typing"):
                    delay = random.uniform(
                        self.settings.typing_delay_min_seconds,
                        self.settings.typing_delay_max_seconds,
                    )
                    await asyncio.sleep(delay)
                    reply = await self.generate(response_prompt(history))
                sent = await event.reply(reply)
                await self.memory.add(
                    group_id,
                    self.me_id,
                    self.me_name,
                    "assistant",
                    reply,
                    created_at=int(sent.date.timestamp()) if sent.date else None,
                )
                self.last_activity[group_id] = time.time()
            except Exception:
                LOGGER.exception("Failed to generate or send a reply in group %s", group_id)

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            removed = await self.memory.purge_expired()
            if removed:
                LOGGER.info("Removed %s expired memory rows", removed)

    def _proactive_interval(self, group_id: int) -> float:
        interval = self.next_proactive_interval.get(group_id)
        if interval is None:
            interval = random.uniform(
                self.settings.proactive_min_interval_minutes * 60,
                self.settings.proactive_max_interval_minutes * 60,
            )
            self.next_proactive_interval[group_id] = interval
        return interval

    async def proactive_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            today = date.today()
            for group_id, last_activity in list(self.last_activity.items()):
                if not self.group_allowed(group_id):
                    continue
                if now - last_activity < self.settings.proactive_idle_minutes * 60:
                    continue
                if now - self.last_proactive.get(group_id, 0) < self._proactive_interval(group_id):
                    continue
                count_key = (group_id, today)
                if self.proactive_counts[count_key] >= self.settings.max_proactive_per_day:
                    continue

                async with self.group_locks[group_id]:
                    history = await self.memory.recent_group(
                        group_id,
                        self.settings.memory_history_limit,
                    )
                    try:
                        message = await self.generate(proactive_prompt(history))
                        sent = await self.client.send_message(group_id, message)
                        await self.memory.add(
                            group_id,
                            self.me_id,
                            self.me_name,
                            "assistant",
                            message,
                            created_at=int(sent.date.timestamp()) if sent.date else None,
                        )
                        self.last_proactive[group_id] = time.time()
                        self.last_activity[group_id] = time.time()
                        self.next_proactive_interval.pop(group_id, None)
                        self.proactive_counts[count_key] += 1
                    except Exception:
                        LOGGER.exception(
                            "Failed to send a proactive message in group %s",
                            group_id,
                        )

    async def run(self) -> None:
        await self.memory.open()
        await self.client.start()
        me = await self.client.get_me()
        if me is None:
            raise RuntimeError("Telegram session did not return an authenticated account")
        self.me_id = int(me.id)
        self.me_name = get_display_name(me) or "這個帳號"

        self.client.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        self.background_tasks.append(asyncio.create_task(self.cleanup_loop()))
        if self.settings.proactive_enabled and self.settings.max_proactive_per_day > 0:
            self.background_tasks.append(asyncio.create_task(self.proactive_loop()))

        role = self.settings.role_key
        groups = (
            ",".join(str(group_id) for group_id in self.settings.group_chat_ids)
            if self.settings.group_chat_ids
            else "all joined groups"
        )
        LOGGER.info(
            "Userbot connected as account_id=%s role=%s groups=%s memory_ttl=%sh",
            self.me_id,
            role,
            groups,
            self.settings.memory_ttl_hours,
        )
        await self.client.run_until_disconnected()

    async def close(self) -> None:
        for task in self.background_tasks:
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        await self.memory.close()
        await self.ai.close()
        await self.client.disconnect()


async def async_main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = UserbotApp(settings)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    runner = asyncio.create_task(app.run())
    stopper = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            {runner, stopper},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if runner in done:
            await runner
    finally:
        stopper.cancel()
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, stopper, return_exceptions=True)
        await app.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except (ValueError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
