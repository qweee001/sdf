import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from telethon.tl.types import MessageMediaPhoto, PhotoEmpty

from app.media import MediaAsset
from app.worker import AccountWorker


class _DB:
    async def get_recent_messages(self, *_args, **_kwargs):
        return []

    async def get_recent_group_replies(self, *_args, **_kwargs):
        return []


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
