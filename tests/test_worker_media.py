import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from telethon.tl.types import MessageMediaPhoto, PhotoEmpty

from app.manager import AccountManager
from app.media import MediaAsset
from app.persona import get_system_prompt
from app.worker import AccountWorker


class _DB:
    def __init__(self):
        self.messages = []
        self.claims = {}

    async def get_recent_messages(self, *_args, **_kwargs):
        return []

    async def get_recent_group_replies(self, *_args, **_kwargs):
        return []

    async def add_message(self, *args):
        self.messages.append(args)

    async def claim_message_response(self, group_id, message_id, account_id):
        key = (group_id, message_id)
        if key in self.claims:
            return False
        self.claims[key] = account_id
        return True

    async def release_message_response_claim(
        self, group_id, message_id, account_id
    ):
        key = (group_id, message_id)
        if self.claims.get(key) != account_id:
            return False
        del self.claims[key]
        return True


class _Event:
    id = 1
    chat_id = -1001
    sender_id = 999
    raw_text = ""
    is_reply = False
    mentioned = False
    reply_to = None
    sender = SimpleNamespace(first_name="阿明", last_name=None)
    media = MessageMediaPhoto(photo=PhotoEmpty(id=1))
    file = SimpleNamespace(size=4, mime_type="image/jpeg")

    async def download_media(self, *, file):
        assert file is bytes
        return b"jpeg"


def _worker(
    media_service=None,
    reply_claim_signals=None,
    failed_reply_claimants=None,
    *,
    uid: str | int = "w1",
    managed_ids=None,
    db=None,
    human_owners=None,
):
    cfg = SimpleNamespace(
        ai_model="test",
        ai_temperature=0.8,
        ai_max_tokens=200,
        ai_timeout=17,
        ai_disable_thinking=True,
        memory_max_messages=10,
        min_typing_delay=0,
        max_typing_delay=0,
        media_enabled=True,
        voice_media_enabled=False,
        media_max_input_bytes=8 * 1024 * 1024,
        base_reply_probability=1.0,
    )
    return AccountWorker(
        account_id=str(uid),
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=db or _DB(),
        config=cfg,
        managed_ids=managed_ids or set(),
        on_status_change=None,
        selected_groups=[-1001],
        media_service=media_service,
        human_owners=human_owners,
        reply_claim_signals=reply_claim_signals,
        failed_reply_claimants=failed_reply_claimants,
    )


def test_media_request_kind_distinguishes_files_from_live_video_calls():
    assert AccountWorker._requested_media_kind("傳張自拍看看") == "image"
    assert AccountWorker._requested_media_kind("可以用語音說嗎") == "voice"
    assert AccountWorker._requested_media_kind("拍個短片給我") == "video"
    assert AccountWorker._requested_media_kind("要不要開視訊") is None


def test_media_claim_coordination_state_is_shared_by_reference():
    signals = {}
    failed = {}
    worker = _worker(
        reply_claim_signals=signals,
        failed_reply_claimants=failed,
    )

    assert worker.reply_claim_signals is signals
    assert worker.failed_reply_claimants is failed


def test_manager_injects_same_media_claim_state_into_worker():
    async def main():
        manager = cast(Any, AccountManager.__new__(AccountManager))
        manager.config = SimpleNamespace(tg_api_id=1, tg_api_hash="hash")
        manager.db = SimpleNamespace(update_account=AsyncMock())
        manager.secret_box = SimpleNamespace(decrypt=lambda value: value)
        manager.workers = {}
        manager._ai_client = None
        manager._media_service = None
        manager._voice_library = SimpleNamespace()
        manager.managed_ids = set()
        manager.active_ids = set()
        manager.active_group_ids = {}
        manager.managed_origins = {}
        manager.human_owners = {}
        manager.recent_proactive_owners = {}
        manager.last_human_activity = {}
        manager.reply_claim_signals = {}
        manager.failed_reply_claimants = {}
        manager.live_test = SimpleNamespace(
            start_block_error=AsyncMock(return_value=""),
            outbound_gate=None,
        )
        fake_worker = SimpleNamespace(start=AsyncMock(), is_running=True)

        with patch("app.manager.AccountWorker", return_value=fake_worker) as factory:
            await manager._start_account({
                "id": "worker-1",
                "session_key": "encrypted-session",
                "groups": "[-1001]",
            })

        kwargs = factory.call_args.kwargs
        assert kwargs["reply_claim_signals"] is manager.reply_claim_signals
        assert kwargs["failed_reply_claimants"] is manager.failed_reply_claimants

    asyncio.run(main())


def test_media_claim_wait_expiry_cleans_shared_state():
    async def main():
        key = (-1001, 77)
        signals = {key: asyncio.Event()}
        failed = {key: {101}}
        worker = _worker(
            reply_claim_signals=signals,
            failed_reply_claimants=failed,
        )
        worker.tg_user_id = 202
        event = _Event()
        event.id = 77

        with patch("app.worker._REPLY_TASK_WINDOW_SECONDS", 0.01):
            assert await worker._wait_for_media_claim(event) is False

        assert signals == {}
        assert failed == {}

    asyncio.run(main())


def test_earlier_waiter_timeout_keeps_later_waiter_attached():
    async def main():
        db = _DB()
        active = {101, 202, 303}
        owners = {(-1001, 78): (101, float("inf"))}
        signals = {}
        failed = {}
        event = _Event()
        event.id = 78

        workers = [
            _worker(
                uid=uid,
                managed_ids=active,
                db=db,
                human_owners=owners,
                reply_claim_signals=signals,
                failed_reply_claimants=failed,
            )
            for uid in sorted(active)
        ]
        for worker, uid in zip(workers, sorted(active)):
            worker.tg_user_id = uid

        with patch("app.worker._REPLY_TASK_WINDOW_SECONDS", 0.08):
            assert await workers[0]._should_reply(event) is True
            earlier = asyncio.create_task(workers[1]._should_reply(event))
            await asyncio.sleep(0.04)
            later = asyncio.create_task(workers[2]._should_reply(event))

            assert await earlier is False
            await workers[0]._finish_media_claim(event, True)
            assert await later is True
            assert db.claims == {(-1001, 78): "303"}

            await workers[2]._finish_media_claim(event, False)

        assert signals == {}
        assert failed == {}

    asyncio.run(main())


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


def test_semantically_blocked_vision_keeps_successful_claim_without_send():
    async def main():
        db = _DB()
        event = _Event()
        event.id = 92
        db.claims[(-1001, 92)] = "w1"
        media = SimpleNamespace(understand_image=AsyncMock(side_effect=[
            "本群管理員很負責",
            "這群絕對不會被騙",
        ]))
        classify = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="BLOCK")
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="BLOCK")
            )]),
        ])
        ai_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=classify))
        )
        client = SimpleNamespace(send_message=AsyncMock())
        worker = _worker(media, db=db)
        worker.ai_client = cast(Any, ai_client)
        worker.tg_client = cast(Any, client)
        worker.tg_user_id = 101
        worker.is_running = True

        await worker._reply_later(event, 0)

        assert media.understand_image.await_count == 2
        assert classify.await_count == 2
        client.send_message.assert_not_awaited()
        assert db.messages == []
        assert db.claims == {(-1001, 92): "w1"}
        assert worker.failed_reply_claimants == {}
        assert worker.stats["reply_drops"]["group_meta"] == 1

    asyncio.run(main())


@pytest.mark.parametrize("account_count", [5, 8])
def test_last_account_owns_image_download_vision_and_send_path(account_count):
    async def main():
        account_ids = (101, 202, 303, 404, 505, 606, 707, 808)
        active_ids = set(account_ids[:account_count])
        owner_id = account_ids[account_count - 1]
        owner_account_id = f"account-{account_count}"
        image_bytes = f"account-{account_count}-image".encode()
        mime_type = {5: "image/jpeg", 8: "image/png"}[account_count]
        vision_reply = f"第{account_count}個帳號看懂了這張照片"
        owners = {(-1001, 999): (owner_id, float("inf"))}
        db = cast(Any, _DB())
        db.claim_message_response = AsyncMock(return_value=True)
        db.claim_group_text = AsyncMock(return_value=True)
        db.touch_activity = AsyncMock()
        media = SimpleNamespace(
            understand_image=AsyncMock(return_value=vision_reply)
        )
        client = SimpleNamespace(send_message=AsyncMock())
        event = SimpleNamespace(
            id=owner_id,
            chat_id=-1001,
            sender_id=999,
            sender=SimpleNamespace(first_name="真人"),
            raw_text="",
            media=MessageMediaPhoto(photo=PhotoEmpty(id=owner_id)),
            file=SimpleNamespace(size=len(image_bytes), mime_type=mime_type),
            mentioned=False,
            is_reply=False,
            reply_to=None,
            download_media=AsyncMock(return_value=image_bytes),
        )

        workers = []
        for index, user_id in enumerate(sorted(active_ids), start=1):
            worker = _worker(media)
            worker.account_id = f"account-{index}"
            worker.tg_user_id = user_id
            worker.managed_ids = active_ids
            worker.active_ids = active_ids
            worker.human_owners = owners
            worker.db = cast(Any, db)
            worker.persona = {
                "name": f"圖片帳號{index}",
                "gender": "女",
                "age": 20 + index,
                "city": "台中",
                "district": "北屯",
                "industry": "設計師",
                "university": "中興",
                "personality": f"第{index}個帳號的個性",
                "hobbies": ["攝影"],
                "looking_for": "想認識人",
                "meetups_done": index,
                "schedule": "正常",
            }
            worker.name = worker.persona["name"]
            worker.tg_client = cast(Any, client)
            worker.is_running = True
            workers.append(worker)

        async def reply_if_owner(worker):
            should_reply = await worker._should_reply(event)
            if should_reply:
                await worker._reply_later(event, 0)
            return should_reply

        decisions = await asyncio.gather(
            *(reply_if_owner(worker) for worker in workers)
        )

        assert decisions.count(True) == 1
        assert decisions[account_count - 1] is True
        assert workers[decisions.index(True)].tg_user_id == owner_id
        event.download_media.assert_awaited_once_with(file=bytes)
        media.understand_image.assert_awaited_once()
        vision_args = media.understand_image.await_args.args
        assert vision_args[:4] == (
            owner_account_id,
            image_bytes,
            mime_type,
            get_system_prompt(workers[-1].persona),
        )
        client.send_message.assert_awaited_once_with(-1001, vision_reply)

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
