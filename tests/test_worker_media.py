from __future__ import annotations

import asyncio
import io
import unittest
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.tl.custom.message import Message
from telethon.tl.types import PeerChannel

from app.content_guard import ContentGuard
from app.media import (
    MediaArtifact,
    MediaIntent,
    MediaKind,
    MediaPolicyError,
    ModerationDecision,
)
from app.media_types import (
    AccountMediaSettings,
    MediaFeatureSettings,
    MediaJob,
    MediaJobReservation,
    MediaQuotaDecision,
)
from app.worker import AccountWorker


GROUP_ID = -1001
SECOND_GROUP_ID = -1002


def feature(
    *,
    enabled: bool = True,
    groups: frozenset[int] = frozenset({GROUP_ID}),
    model: str = "",
    voice: str = "",
    daily_limit: int = 5,
    cooldown_seconds: int = 30,
) -> MediaFeatureSettings:
    return MediaFeatureSettings(
        enabled=enabled,
        model=model,
        voice=voice,
        daily_limit=daily_limit,
        cooldown_seconds=cooldown_seconds,
        allowed_group_ids=groups,
    )


def media_settings(
    *,
    image: MediaFeatureSettings | None = None,
    voice: MediaFeatureSettings | None = None,
    video: MediaFeatureSettings | None = None,
) -> AccountMediaSettings:
    return AccountMediaSettings(
        image=image or MediaFeatureSettings(),
        voice=voice or MediaFeatureSettings(),
        video=video or MediaFeatureSettings(),
    )


def media_job(
    kind: str,
    *,
    text: str = "",
    prompt: str = "",
    group_id: int = GROUP_ID,
    voice: str = "",
) -> MediaJob:
    return MediaJob(
        id=f"job-{kind}",
        account_id="account-1",
        group_id=group_id,
        media_type=kind,
        status="running",
        payload={
            "text": text,
            "prompt": prompt,
            "source_message_id": 321,
            "voice": voice,
        },
        result_ref="",
        error="",
        attempts=1,
        available_at=0,
        created_at=0,
        updated_at=0,
    )


def telegram_message(message_id: int = 777) -> Message:
    return Message(
        id=message_id,
        peer_id=PeerChannel(1001),
        date=datetime.now(timezone.utc),
    )


def make_worker(
    account_media: AccountMediaSettings,
    *,
    account_groups: frozenset[int] = frozenset(
        {GROUP_ID, SECOND_GROUP_ID}
    ),
) -> AccountWorker:
    worker = AccountWorker.__new__(AccountWorker)
    worker.account = SimpleNamespace(
        id="account-1",
        label="Account 1",
        all_groups=False,
        group_ids=account_groups,
        media_settings=account_media,
        role_key="female_old_member",
        style="自然",
        gender="female",
        blocked_terms=(),
        blocked_topics=(),
        typing_delay_min_seconds=0,
        typing_delay_max_seconds=0,
    )
    worker.settings = SimpleNamespace(memory_history_limit=30)
    worker.content_guard = ContentGuard()
    worker.media_service = SimpleNamespace()
    worker.store = SimpleNamespace()
    worker.client = SimpleNamespace()
    worker.managed_ids_provider = lambda: frozenset()
    worker.me_id = 999
    worker.me_name = "測試帳號"
    worker.group_locks = defaultdict(asyncio.Lock)
    worker.last_activity = {}
    worker.media_jobs_queued = 0
    worker.media_sent = 0
    worker.media_failed = 0
    worker.replies_sent = 0
    worker.errors = 0
    worker.blocked_messages = 0
    worker.policy_rejections = 0
    worker.last_error = ""
    return worker


class WorkerMediaIntentTests(unittest.TestCase):
    def test_structured_intent_only_uses_media_enabled_for_the_group(
        self,
    ) -> None:
        async def scenario() -> None:
            worker = make_worker(
                media_settings(
                    image=feature(groups=frozenset({GROUP_ID})),
                    video=feature(groups=frozenset({SECOND_GROUP_ID})),
                )
            )
            worker._completion = AsyncMock(  # type: ignore[method-assign]
                return_value=(
                    '{"type":"image","text":"夜景",'
                    '"prompt":"台北夜景"}'
                )
            )

            detected = await worker._detect_media_intent(
                GROUP_ID,
                "幫我做一張夜景圖",
                [],
            )

            self.assertEqual(
                detected,
                MediaIntent(MediaKind.IMAGE, "夜景", "台北夜景"),
            )
            self.assertEqual(
                worker._allowed_media_kinds(GROUP_ID),
                frozenset({MediaKind.IMAGE}),
            )

            worker._completion.reset_mock()
            self.assertIsNone(
                await worker._detect_media_intent(
                    -9999,
                    "幫我做一張圖",
                    [],
                )
            )
            worker._completion.assert_not_awaited()

            worker._completion.return_value = (
                '{"type":"video","text":null,"prompt":"夜景"}'
            )
            self.assertIsNone(
                await worker._detect_media_intent(
                    GROUP_ID,
                    "幫我做影片",
                    [],
                )
            )

        asyncio.run(scenario())

    def test_on_message_passes_the_actual_source_message_id_to_queue(
        self,
    ) -> None:
        async def scenario() -> None:
            worker = make_worker(
                media_settings(image=feature())
            )
            worker.store = SimpleNamespace(
                add=AsyncMock(),
                recent_group=AsyncMock(return_value=[]),
            )
            worker.should_reply = AsyncMock(  # type: ignore[method-assign]
                return_value=True
            )
            intent = MediaIntent(
                MediaKind.IMAGE,
                "夜景",
                "台北夜景",
            )
            worker._detect_media_intent = AsyncMock(  # type: ignore[method-assign]
                return_value=intent
            )
            worker._queue_media_intent = AsyncMock(  # type: ignore[method-assign]
                return_value=True
            )
            event = SimpleNamespace(
                is_group=True,
                chat_id=GROUP_ID,
                raw_text="幫我做一張夜景圖",
                sender_id=123,
                message=SimpleNamespace(id=654),
                get_sender=AsyncMock(return_value=None),
            )

            await worker.on_message(event)

            worker._queue_media_intent.assert_awaited_once_with(
                GROUP_ID,
                654,
                intent,
            )

        asyncio.run(scenario())

    def test_quota_enqueue_is_atomic_and_rechecks_account_group_scope(
        self,
    ) -> None:
        async def scenario() -> None:
            image_feature = feature(
                daily_limit=7,
                cooldown_seconds=45,
                voice="",
            )
            worker = make_worker(
                media_settings(image=image_feature),
                account_groups=frozenset({GROUP_ID}),
            )
            intent = MediaIntent(
                MediaKind.IMAGE,
                "夜景",
                "台北夜景",
            )
            quota = MediaQuotaDecision(
                allowed=True,
                reason="allowed",
                used=1,
                remaining=6,
                retry_after_seconds=0,
            )
            reservation = MediaJobReservation(
                quota=quota,
                job=media_job(
                    "image",
                    text="夜景",
                    prompt="台北夜景",
                ),
            )
            enqueue = AsyncMock(return_value=reservation)
            worker.store = SimpleNamespace(enqueue_media_job=enqueue)

            self.assertTrue(
                await worker._queue_media_intent(
                    GROUP_ID,
                    654,
                    intent,
                )
            )
            enqueue.assert_awaited_once_with(
                "account-1",
                GROUP_ID,
                "image",
                {
                    "text": "夜景",
                    "prompt": "台北夜景",
                    "source_message_id": 654,
                    "voice": "",
                },
                daily_limit=7,
                cooldown_seconds=45,
            )
            self.assertEqual(worker.media_jobs_queued, 1)

            enqueue.reset_mock()
            self.assertFalse(
                await worker._queue_media_intent(
                    SECOND_GROUP_ID,
                    655,
                    intent,
                )
            )
            enqueue.assert_not_awaited()
            self.assertFalse(
                await worker._queue_media_intent(
                    GROUP_ID,
                    656,
                    MediaIntent(MediaKind.TEXT, text="一般文字"),
                )
            )
            enqueue.assert_not_awaited()

            enqueue.return_value = MediaJobReservation(
                quota=MediaQuotaDecision(
                    allowed=False,
                    reason="daily_limit",
                    used=7,
                    remaining=0,
                    retry_after_seconds=100,
                ),
                job=None,
            )
            self.assertFalse(
                await worker._queue_media_intent(
                    GROUP_ID,
                    657,
                    intent,
                )
            )
            self.assertEqual(worker.media_jobs_queued, 1)

        asyncio.run(scenario())


class WorkerMediaSafetyTests(unittest.TestCase):
    def test_image_and_video_use_prompt_preflight_and_preview_postflight(
        self,
    ) -> None:
        async def run_kind(kind: MediaKind) -> None:
            worker = make_worker(
                media_settings(
                    image=feature(),
                    video=feature(),
                )
            )
            data = b"image" if kind is MediaKind.IMAGE else b"video"
            preview = data if kind is MediaKind.IMAGE else b"spritesheet"
            content_type = (
                "image/png"
                if kind is MediaKind.IMAGE
                else "video/mp4"
            )
            artifact = MediaArtifact(
                kind=kind,
                text="安全字幕",
                data=data,
                content_type=content_type,
                filename=(
                    "image.png"
                    if kind is MediaKind.IMAGE
                    else "video.mp4"
                ),
                safety_preview=preview,
                safety_preview_content_type="image/png",
                safety_preview_variant=(
                    "image"
                    if kind is MediaKind.IMAGE
                    else "spritesheet"
                ),
            )
            service = SimpleNamespace(
                moderation_text=AsyncMock(
                    return_value=ModerationDecision(False)
                ),
                moderation_image=AsyncMock(
                    return_value=ModerationDecision(False)
                ),
                render=AsyncMock(return_value=artifact),
                synthesize_voice=AsyncMock(),
            )
            worker.media_service = service
            worker._verify_media_text = AsyncMock(  # type: ignore[method-assign]
                return_value="安全字幕"
            )
            job = media_job(
                kind.value,
                text="安全字幕",
                prompt="台北夜景",
            )

            returned, caption = await worker._render_media_job(job)

            self.assertIs(returned, artifact)
            self.assertEqual(caption, "安全字幕")
            service.moderation_text.assert_awaited_once_with("台北夜景")
            service.render.assert_awaited_once()
            service.moderation_image.assert_awaited_once_with(
                preview,
                "image/png",
            )

        async def scenario() -> None:
            await run_kind(MediaKind.IMAGE)
            await run_kind(MediaKind.VIDEO)

        asyncio.run(scenario())

    def test_preflight_and_postflight_rejections_never_release_media(
        self,
    ) -> None:
        async def scenario() -> None:
            worker = make_worker(
                media_settings(image=feature())
            )
            artifact = MediaArtifact(
                kind=MediaKind.IMAGE,
                text="",
                data=b"image",
                content_type="image/png",
                filename="image.png",
                safety_preview=b"image",
                safety_preview_content_type="image/png",
                safety_preview_variant="image",
            )
            service = SimpleNamespace(
                moderation_text=AsyncMock(
                    return_value=ModerationDecision(True, ("sexual/minors",))
                ),
                moderation_image=AsyncMock(),
                render=AsyncMock(return_value=artifact),
                synthesize_voice=AsyncMock(),
            )
            worker.media_service = service
            job = media_job("image", prompt="不安全提示")

            with self.assertRaisesRegex(
                MediaPolicyError,
                "prompt was rejected",
            ):
                await worker._render_media_job(job)
            service.render.assert_not_awaited()
            service.moderation_image.assert_not_awaited()

            service.moderation_text.return_value = ModerationDecision(False)
            service.moderation_image.return_value = ModerationDecision(
                True,
                ("violence",),
            )
            with self.assertRaisesRegex(
                MediaPolicyError,
                "Generated media was rejected",
            ):
                await worker._render_media_job(job)
            service.render.assert_awaited_once()
            service.moderation_image.assert_awaited_once()

        asyncio.run(scenario())

    def test_mismatched_artifact_and_image_preview_are_fail_closed(self) -> None:
        async def scenario() -> None:
            worker = make_worker(
                media_settings(image=feature())
            )
            service = SimpleNamespace(
                moderation_text=AsyncMock(
                    return_value=ModerationDecision(False)
                ),
                moderation_image=AsyncMock(
                    return_value=ModerationDecision(False)
                ),
                render=AsyncMock(),
                synthesize_voice=AsyncMock(),
            )
            worker.media_service = service
            job = media_job("image", prompt="安全提示")

            service.render.return_value = MediaArtifact(
                kind=MediaKind.VOICE,
                text="",
                data=b"not-an-image",
                content_type="audio/ogg",
                filename="voice.ogg",
            )
            with self.assertRaisesRegex(
                MediaPolicyError,
                "mismatched artifact",
            ):
                await worker._render_media_job(job)
            service.moderation_image.assert_not_awaited()

            service.render.return_value = MediaArtifact(
                kind=MediaKind.IMAGE,
                text="",
                data=b"sent-image",
                content_type="image/png",
                filename="image.png",
                safety_preview=b"different-image",
                safety_preview_content_type="image/png",
                safety_preview_variant="image",
            )
            with self.assertRaisesRegex(
                MediaPolicyError,
                "preview did not match",
            ):
                await worker._render_media_job(job)
            service.moderation_image.assert_not_awaited()

        asyncio.run(scenario())


class WorkerMediaSendTests(unittest.TestCase):
    def test_voice_is_sent_by_this_worker_client_as_a_voice_note(self) -> None:
        async def scenario() -> None:
            worker = make_worker(
                media_settings(voice=feature(voice="zh-TW-HsiaoChenNeural"))
            )
            artifact = MediaArtifact(
                kind=MediaKind.VOICE,
                text="晚安",
                data=b"OggS-voice",
                content_type="audio/ogg",
                filename="voice.ogg",
            )
            worker._render_media_job = AsyncMock(  # type: ignore[method-assign]
                return_value=(artifact, "晚安")
            )
            sent_message = telegram_message(801)
            worker.client = SimpleNamespace(
                send_file=AsyncMock(return_value=sent_message)
            )
            other_client = SimpleNamespace(send_file=AsyncMock())
            worker.store = SimpleNamespace(add=AsyncMock())

            message_id = await worker._send_media_job(
                media_job(
                    "voice",
                    text="晚安",
                    voice="zh-TW-HsiaoChenNeural",
                )
            )

            self.assertEqual(message_id, 801)
            worker.client.send_file.assert_awaited_once()
            other_client.send_file.assert_not_awaited()
            group_id, = worker.client.send_file.await_args.args
            kwargs = worker.client.send_file.await_args.kwargs
            self.assertEqual(group_id, GROUP_ID)
            self.assertTrue(kwargs["voice_note"])
            self.assertNotIn("supports_streaming", kwargs)
            self.assertEqual(kwargs["caption"], "晚安")
            self.assertEqual(kwargs["reply_to"], 321)
            self.assertIsInstance(kwargs["file"], io.BytesIO)
            self.assertEqual(kwargs["file"].name, "voice.ogg")
            self.assertEqual(kwargs["file"].getvalue(), b"OggS-voice")
            worker.store.add.assert_awaited_once()
            self.assertEqual(worker.media_sent, 1)
            self.assertEqual(worker.replies_sent, 1)

        asyncio.run(scenario())

    def test_video_generation_does_not_hold_the_group_lock(self) -> None:
        async def scenario() -> None:
            worker = make_worker(
                media_settings(video=feature())
            )
            lock = worker.group_locks[GROUP_ID]
            artifact = MediaArtifact(
                kind=MediaKind.VIDEO,
                text="短片",
                data=b"mp4",
                content_type="video/mp4",
                filename="video.mp4",
                safety_preview=b"sheet",
                safety_preview_content_type="image/jpeg",
                safety_preview_variant="spritesheet",
            )

            async def render(_job: MediaJob) -> tuple[MediaArtifact, str]:
                self.assertFalse(lock.locked())
                await asyncio.sleep(0)
                self.assertFalse(lock.locked())
                return artifact, "短片"

            async def send_file(
                _group_id: int,
                **_kwargs: object,
            ) -> Message:
                self.assertTrue(lock.locked())
                return telegram_message(802)

            worker._render_media_job = render  # type: ignore[method-assign]
            worker.client = SimpleNamespace(send_file=AsyncMock(side_effect=send_file))
            worker.store = SimpleNamespace(add=AsyncMock())

            message_id = await worker._send_media_job(
                media_job("video", text="短片", prompt="海邊")
            )

            self.assertEqual(message_id, 802)
            self.assertFalse(lock.locked())
            kwargs = worker.client.send_file.await_args.kwargs
            self.assertTrue(kwargs["supports_streaming"])
            self.assertNotIn("voice_note", kwargs)

        asyncio.run(scenario())


class WorkerMediaJobStateTests(unittest.TestCase):
    @staticmethod
    def loop_store(job: MediaJob) -> SimpleNamespace:
        return SimpleNamespace(
            recover_stale_media_jobs=AsyncMock(return_value=0),
            claim_next_media_job=AsyncMock(
                side_effect=[job, asyncio.CancelledError()]
            ),
            finish_media_job=AsyncMock(),
        )

    def test_successful_job_is_completed_with_worker_telegram_reference(
        self,
    ) -> None:
        async def scenario() -> None:
            worker = make_worker(
                media_settings(image=feature())
            )
            job = media_job("image", prompt="夜景")
            worker.store = self.loop_store(job)
            worker._send_media_job = AsyncMock(  # type: ignore[method-assign]
                return_value=901
            )

            with self.assertRaises(asyncio.CancelledError):
                await worker.media_loop()

            worker.store.recover_stale_media_jobs.assert_awaited_once_with(
                "account-1"
            )
            worker.store.finish_media_job.assert_awaited_once_with(
                job.id,
                "completed",
                result_ref="telegram:901",
            )

        asyncio.run(scenario())

    def test_safety_rejection_finishes_job_with_generic_failed_state(
        self,
    ) -> None:
        async def scenario() -> None:
            worker = make_worker(
                media_settings(video=feature())
            )
            job = media_job("video", prompt="blocked")
            worker.store = self.loop_store(job)
            worker._send_media_job = AsyncMock(  # type: ignore[method-assign]
                side_effect=MediaPolicyError(
                    "sensitive provider detail must not persist"
                )
            )

            with self.assertRaises(asyncio.CancelledError):
                await worker.media_loop()

            worker.store.finish_media_job.assert_awaited_once_with(
                job.id,
                "failed",
                error="blocked by media safety policy",
            )
            self.assertEqual(worker.blocked_messages, 1)
            self.assertEqual(worker.media_failed, 1)
            finish_kwargs = worker.store.finish_media_job.await_args.kwargs
            self.assertNotIn(
                "sensitive provider detail",
                finish_kwargs["error"],
            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
