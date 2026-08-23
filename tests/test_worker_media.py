import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from telethon.tl.types import MessageMediaPhoto, PhotoEmpty

from app.media import MediaAsset
from app.worker import AccountWorker


class _DB:
    def __init__(self):
        self.messages = []

    async def get_recent_messages(self, *_args, **_kwargs):
        return []

    async def get_recent_group_replies(self, *_args, **_kwargs):
        return []

    async def add_message(self, *args):
        self.messages.append(args)

    async def claim_message_response(self, *_args, **_kwargs):
        return False


class _Event:
    chat_id = -1001
    sender_id = 999
    raw_text = ""
    sender = SimpleNamespace(first_name="阿明", last_name=None)
    media = MessageMediaPhoto(photo=PhotoEmpty(id=1))
    file = SimpleNamespace(size=4, mime_type="image/jpeg")

    async def download_media(self, *, file):
        assert file is bytes
        return b"jpeg"


def _worker(media_service=None):
    cfg = SimpleNamespace(
        ai_model="test",
        memory_max_messages=10,
        min_typing_delay=0,
        max_typing_delay=0,
        media_enabled=True,
        voice_media_enabled=False,
        media_max_input_bytes=8 * 1024 * 1024,
    )
    return AccountWorker(
        account_id="w1",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=_DB(),
        config=cfg,
        managed_ids=set(),
        on_status_change=None,
        selected_groups=[-1001],
        media_service=media_service,
    )


def test_media_request_kind_distinguishes_files_from_live_video_calls():
    assert AccountWorker._requested_media_kind("傳張自拍看看") == "image"
    assert AccountWorker._requested_media_kind("可以用語音說嗎") == "voice"
    assert AccountWorker._requested_media_kind("拍個短片給我") == "video"
    assert AccountWorker._requested_media_kind("要不要開視訊") is None


def test_incoming_image_is_downloaded_only_for_vision_reply():
    async def main():
        media = SimpleNamespace(
            understand_image=AsyncMock(return_value="這杯咖啡看起來不錯欸")
        )
        worker = _worker(media)
        reply = await worker._generate_reply(_Event())
        assert reply == "這杯咖啡看起來不錯欸"
        args = media.understand_image.await_args.args
        assert args[0] == "w1"
        assert args[1] == b"jpeg"
        assert args[2] == "image/jpeg"
        assert "[圖片]" in args[4]
        assert worker.stats["images_seen"] == 1
        assert worker.stats["images_understood"] == 1
        assert worker.stats["image_understanding_errors"] == 0

    asyncio.run(main())


def test_incoming_image_is_saved_to_context_with_image_marker():
    async def main():
        db = _DB()
        worker = _worker()
        worker.db = cast(Any, db)
        worker.tg_client = cast(Any, object())
        worker.is_running = True
        event = cast(Any, _Event())
        event.id = 41
        event.is_private = False
        event.is_group = True
        event.mentioned = False
        event.is_reply = False
        event.reply_to = None
        event.get_sender = AsyncMock(return_value=event.sender)

        await worker.on_message(event)

        assert db.messages[0][-1] == "[圖片]"

    asyncio.run(main())


def test_empty_vision_result_is_not_replaced_with_fake_seen_reply():
    async def main():
        media = SimpleNamespace(understand_image=AsyncMock(return_value=""))
        worker = _worker(media)
        event = _Event()
        assert await worker._generate_reply(event) == ""
        assert worker._take_generation_reason(event) == "image_understanding_empty"
        assert worker.stats["images_seen"] == 1
        assert worker.stats["images_understood"] == 0
        assert worker.stats["image_understanding_errors"] == 1

    asyncio.run(main())


def test_media_switch_off_during_vision_prevents_late_reply():
    async def main():
        worker = _worker()

        async def understand(*_args):
            worker.config.media_enabled = False
            return "照片裡是一杯咖啡"

        worker.media_service = cast(
            Any,
            SimpleNamespace(understand_image=AsyncMock(side_effect=understand)),
        )
        event = _Event()
        assert await worker._generate_reply(event) == ""
        assert worker._take_generation_reason(event) == "media_disabled"
        assert worker.stats["images_understood"] == 0

    asyncio.run(main())


def test_voice_request_never_calls_tts_while_voice_is_disabled():
    async def main():
        media = SimpleNamespace(generate_voice=AsyncMock())
        worker = _worker(media)
        event = _Event()
        event.raw_text = "可以用語音回我嗎"
        assert await worker._generate_requested_media(event, "voice") is None
        media.generate_voice.assert_not_awaited()
        assert worker.stats["voice_blocked"] == 1

    asyncio.run(main())


def test_oversized_image_is_not_downloaded_or_sent_to_model():
    async def main():
        media = SimpleNamespace(understand_image=AsyncMock())
        worker = _worker(media)
        event = _Event()
        event.file = SimpleNamespace(size=9 * 1024 * 1024, mime_type="image/jpeg")
        event.download_media = AsyncMock(return_value=b"x")
        assert await worker._generate_reply(event) == ""
        event.download_media.assert_not_awaited()
        media.understand_image.assert_not_awaited()

    asyncio.run(main())


def test_send_media_uses_telegram_native_flags():
    async def main():
        worker = _worker()
        worker.config.voice_media_enabled = True
        client = SimpleNamespace(send_file=AsyncMock())
        worker.tg_client = cast(Any, client)
        worker.is_running = True

        voice = MediaAsset("voice", b"opus", "voice.ogg", "audio/ogg")
        video = MediaAsset("video", b"mp4", "video.mp4", "video/mp4")
        image = MediaAsset("image", b"png", "image.png", "image/png")
        assert await worker._send_media(-1001, voice)
        assert await worker._send_media(-1001, video)
        assert await worker._send_media(-1001, image)

        calls = client.send_file.await_args_list
        assert calls[0].kwargs["voice_note"] is True
        assert calls[1].kwargs["supports_streaming"] is True
        assert calls[2].kwargs.get("voice_note") is not True

    asyncio.run(main())


def test_send_media_rejects_voice_even_if_asset_reaches_send_layer():
    async def main():
        worker = _worker()
        client = SimpleNamespace(send_file=AsyncMock())
        worker.tg_client = cast(Any, client)
        worker.is_running = True
        voice = MediaAsset("voice", b"opus", "voice.ogg", "audio/ogg")
        assert await worker._send_media(-1001, voice) is False
        client.send_file.assert_not_awaited()

    asyncio.run(main())


def test_media_switch_rechecks_after_typing_delay_before_text_side_effect():
    async def main():
        worker = _worker()
        client = SimpleNamespace(send_message=AsyncMock())
        worker.tg_client = cast(Any, client)
        worker.is_running = True

        async def switch_off(_seconds):
            worker.config.media_enabled = False

        with patch("app.worker.asyncio.sleep", side_effect=switch_off):
            sent = await worker._send_message_unlocked(
                -1001,
                "這杯咖啡看起來不錯欸",
                require_media_enabled=True,
            )
        assert sent is False
        client.send_message.assert_not_awaited()

    asyncio.run(main())
