from __future__ import annotations

import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.media import (
    AZURE_FEMALE_VOICE,
    AZURE_MALE_VOICE,
    AZURE_OPUS_OUTPUT_FORMAT,
    AsyncMediaQueue,
    FIXED_MEDIA_SAFETY_POLICY,
    HTTPResponse,
    HttpxAsyncTransport,
    MediaCancelledError,
    MediaIntent,
    MediaIntentError,
    MediaKind,
    MediaPolicyError,
    MediaProviderError,
    MediaQueueFullError,
    MediaSafetyGate,
    MediaService,
    MediaSettings,
    MediaTimeoutError,
    MediaTooLargeError,
    parse_media_intent,
)


class FakeEndpoint:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    async def generate(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeOpenAI:
    def __init__(
        self,
        *,
        image_results: tuple[object, ...] = (),
        moderation_results: tuple[object, ...] = (),
    ) -> None:
        self.images = FakeEndpoint(*image_results)
        self.moderations = FakeEndpoint(*moderation_results)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: object = None,
        params: object = None,
        content: object = None,
        multipart: object = None,
        timeout: float,
        max_bytes: int,
    ) -> HTTPResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "content": content,
                "multipart": multipart,
                "timeout": timeout,
                "max_bytes": max_bytes,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]

    async def close(self) -> None:
        self.closed = True


def json_response(payload: object, status: int = 200) -> HTTPResponse:
    return HTTPResponse(
        status_code=status,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload).encode(),
    )


def image_response(data: bytes) -> SimpleNamespace:
    return SimpleNamespace(
        data=[
            SimpleNamespace(
                b64_json=base64.b64encode(data).decode("ascii"),
            )
        ]
    )


def moderation_response(
    flagged: object,
    categories: object = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        results=[
            SimpleNamespace(
                flagged=flagged,
                categories={} if categories is None else categories,
            )
        ]
    )


def media_settings(**overrides: object) -> MediaSettings:
    values: dict[str, object] = {
        "openai_api_key": "openai-test-key",
        "openai_base_url": "https://api.openai.test/v1",
        "azure_speech_key": "azure-test-key",
        "azure_speech_region": "eastasia",
        "video_poll_interval_seconds": 0,
        "video_timeout_seconds": 30,
        "max_concurrency": 2,
        "max_queued_jobs": 4,
    }
    values.update(overrides)
    return MediaSettings(**values)


class MediaIntentTests(unittest.TestCase):
    def test_parses_all_four_strict_intents(self) -> None:
        cases = (
            (
                '{"type":"text","text":"  嗨  ","prompt":null}',
                MediaIntent(MediaKind.TEXT, "嗨", ""),
            ),
            (
                '{"type":"voice","text":"晚安","prompt":null}',
                MediaIntent(MediaKind.VOICE, "晚安", ""),
            ),
            (
                '{"type":"image","text":null,"prompt":" 夜景 "}',
                MediaIntent(MediaKind.IMAGE, "", "夜景"),
            ),
            (
                '{"type":"video","text":"短片","prompt":" 海邊散步 "}',
                MediaIntent(MediaKind.VIDEO, "短片", "海邊散步"),
            ),
        )

        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(parse_media_intent(raw), expected)

    def test_invalid_or_non_exact_json_fails_closed(self) -> None:
        invalid = (
            "",
            "not-json",
            '```json\n{"type":"text","text":"hi","prompt":null}\n```',
            '{"type":"text","text":"hi","prompt":null} trailing',
            '["text","hi"]',
            '{"type":"text","text":"hi"}',
            '{"type":"text","text":"hi","prompt":null,"extra":1}',
            '{"type":"text","type":"voice","text":"hi","prompt":null}',
            '{"type":"TEXT","text":"hi","prompt":null}',
            '{"type":"text","text":"","prompt":null}',
            '{"type":"text","text":"hi","prompt":""}',
            '{"type":"voice","text":12,"prompt":null}',
            '{"type":"image","text":null,"prompt":""}',
            '{"type":"video","text":null,"prompt":false}',
        )

        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(MediaIntentError):
                    parse_media_intent(raw)


class MediaSafetyAndQueueTests(unittest.TestCase):
    def test_fixed_and_custom_rules_block_before_review_hook(self) -> None:
        async def scenario() -> None:
            hook = AsyncMock(return_value=True)
            gate = MediaSafetyGate(
                blocked_terms=("禁止詞",),
                review_hook=hook,
            )

            for prompt in ("兒童色情內容", "含有禁止詞的內容"):
                with self.subTest(prompt=prompt):
                    with self.assertRaises(MediaPolicyError):
                        await gate.ensure_allowed(
                            MediaIntent(MediaKind.IMAGE, prompt=prompt)
                        )

            hook.assert_not_awaited()

        asyncio.run(scenario())

    def test_review_hook_receives_fixed_policy_and_fails_closed(self) -> None:
        async def scenario() -> None:
            reviews: list[object] = []

            async def allow(review: object) -> bool:
                reviews.append(review)
                return True

            gate = MediaSafetyGate(review_hook=allow)
            intent = MediaIntent(MediaKind.VIDEO, prompt="海邊散步")
            await gate.ensure_allowed(intent)

            self.assertEqual(len(reviews), 1)
            self.assertEqual(reviews[0].policy, FIXED_MEDIA_SAFETY_POLICY)
            self.assertEqual(reviews[0].intent, intent)

            rejecting = MediaSafetyGate(review_hook=AsyncMock(return_value=False))
            with self.assertRaises(MediaPolicyError):
                await rejecting.ensure_allowed(intent)

            async def broken(_review: object) -> bool:
                raise RuntimeError("classifier unavailable")

            unavailable = MediaSafetyGate(review_hook=broken)
            with self.assertRaisesRegex(
                MediaPolicyError,
                "could not be completed",
            ):
                await unavailable.ensure_allowed(intent)

        asyncio.run(scenario())

    def test_queue_is_bounded_and_cancellation_releases_capacity(self) -> None:
        async def scenario() -> None:
            queue = AsyncMediaQueue(max_concurrency=1, max_jobs=1)
            entered = asyncio.Event()

            async def blocking() -> None:
                entered.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(queue.run(blocking))
            await entered.wait()
            self.assertEqual(queue.pending_jobs, 1)

            with self.assertRaises(MediaQueueFullError):
                await queue.run(lambda: asyncio.sleep(0))

            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(queue.pending_jobs, 0)
            self.assertIsNone(
                await queue.run(lambda: asyncio.sleep(0))
            )

        asyncio.run(scenario())


class HTTPTransportTests(unittest.TestCase):
    def test_owned_http_client_never_follows_provider_redirects(self) -> None:
        async def scenario() -> None:
            transport = HttpxAsyncTransport()
            try:
                self.assertFalse(transport._client.follow_redirects)
            finally:
                await transport.close()

        asyncio.run(scenario())

    def test_httpx_transport_builds_multipart_form_and_streams_bytes(
        self,
    ) -> None:
        async def scenario() -> None:
            captured: dict[str, object] = {}

            async def handler(request: httpx.Request) -> httpx.Response:
                captured["headers"] = dict(request.headers)
                captured["body"] = await request.aread()
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    content=b'{"status":"queued"}',
                )

            client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            )
            transport = HttpxAsyncTransport(client)
            try:
                response = await transport.request(
                    "POST",
                    "https://api.openai.test/v1/videos",
                    multipart={
                        "model": "sora-2",
                        "prompt": "海邊散步",
                    },
                    timeout=5,
                    max_bytes=1024,
                )
            finally:
                await client.aclose()

            self.assertEqual(response.body, b'{"status":"queued"}')
            content_type = captured["headers"]["content-type"]
            self.assertTrue(
                content_type.startswith("multipart/form-data; boundary=")
            )
            body = captured["body"]
            self.assertIn(b'name="model"', body)
            self.assertIn(b"sora-2", body)
            self.assertIn(b'name="prompt"', body)
            self.assertIn("海邊散步".encode(), body)

        asyncio.run(scenario())

    def test_httpx_transport_rejects_declared_oversized_response(self) -> None:
        async def scenario() -> None:
            async def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"Content-Length": "100"},
                    content=b"x" * 100,
                )

            client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            )
            transport = HttpxAsyncTransport(client)
            try:
                with self.assertRaises(MediaTooLargeError):
                    await transport.request(
                        "GET",
                        "https://api.openai.test/v1/videos/video_1/content",
                        timeout=5,
                        max_bytes=10,
                    )
            finally:
                await client.aclose()

        asyncio.run(scenario())


class MediaProviderTests(unittest.TestCase):
    def test_openai_image_returns_bytes_and_post_moderation_preview(self) -> None:
        async def scenario() -> None:
            expected = b"\x89PNG\r\nimage"
            client = FakeOpenAI(image_results=(image_response(expected),))
            service = MediaService(
                media_settings(),
                openai_client=client,
                http_transport=FakeTransport(),
            )

            artifact = await service.generate_image(
                "台北夜景",
                "今晚的城市",
            )

            self.assertEqual(artifact.kind, MediaKind.IMAGE)
            self.assertEqual(artifact.data, expected)
            self.assertEqual(artifact.safety_preview, expected)
            self.assertEqual(
                artifact.safety_preview_content_type,
                "image/png",
            )
            self.assertEqual(artifact.text, "今晚的城市")
            self.assertEqual(
                client.images.calls,
                [
                    {
                        "model": "gpt-image-1",
                        "prompt": "台北夜景",
                        "n": 1,
                        "output_format": "png",
                        "moderation": "auto",
                    }
                ],
            )

        asyncio.run(scenario())

    def test_dall_e_requests_base64_without_gpt_image_options(self) -> None:
        async def scenario() -> None:
            client = FakeOpenAI(image_results=(image_response(b"png"),))
            service = MediaService(
                media_settings(
                    image_model="dall-e-3",
                    image_output_format="webp",
                ),
                openai_client=client,
                http_transport=FakeTransport(),
            )

            artifact = await service.generate_image("台北夜景")

            self.assertEqual(
                client.images.calls,
                [
                    {
                        "model": "dall-e-3",
                        "prompt": "台北夜景",
                        "n": 1,
                        "response_format": "b64_json",
                    }
                ],
            )
            self.assertEqual(artifact.content_type, "image/png")
            self.assertEqual(artifact.filename, "image.png")

        asyncio.run(scenario())

    def test_invalid_or_oversized_image_is_rejected(self) -> None:
        async def scenario() -> None:
            invalid = SimpleNamespace(
                data=[SimpleNamespace(b64_json="not base64 !")]
            )
            invalid_service = MediaService(
                media_settings(),
                openai_client=FakeOpenAI(image_results=(invalid,)),
                http_transport=FakeTransport(),
            )
            with self.assertRaises(MediaProviderError):
                await invalid_service.generate_image("正常提示")

            large_service = MediaService(
                media_settings(max_image_bytes=3),
                openai_client=FakeOpenAI(
                    image_results=(image_response(b"four"),)
                ),
                http_transport=FakeTransport(),
            )
            with self.assertRaises(MediaTooLargeError):
                await large_service.generate_image("正常提示")

        asyncio.run(scenario())

    def test_azure_tts_uses_taiwan_voices_ssml_and_ogg_opus(self) -> None:
        async def scenario() -> None:
            transport = FakeTransport(
                HTTPResponse(200, {"Content-Type": "audio/ogg"}, b"OggS-f"),
                HTTPResponse(200, {"Content-Type": "audio/ogg"}, b"OggS-m"),
            )
            service = MediaService(
                media_settings(),
                openai_client=FakeOpenAI(),
                http_transport=transport,
            )

            female = await service.synthesize_voice(
                "妳好 & <晚安>",
                gender="female",
            )
            male = await service.synthesize_voice("你好", gender="male")

            self.assertEqual(female.data, b"OggS-f")
            self.assertEqual(male.data, b"OggS-m")
            self.assertEqual(female.content_type, "audio/ogg")
            female_call, male_call = transport.calls
            self.assertEqual(
                female_call["headers"]["X-Microsoft-OutputFormat"],
                AZURE_OPUS_OUTPUT_FORMAT,
            )
            self.assertEqual(
                female_call["headers"]["Ocp-Apim-Subscription-Key"],
                "azure-test-key",
            )
            self.assertIn(AZURE_FEMALE_VOICE, female_call["content"].decode())
            self.assertIn("&amp;", female_call["content"].decode())
            self.assertIn("&lt;晚安&gt;", female_call["content"].decode())
            self.assertIn(AZURE_MALE_VOICE, male_call["content"].decode())
            self.assertEqual(
                female_call["url"],
                "https://eastasia.tts.speech.microsoft.com/"
                "cognitiveservices/v1",
            )

        asyncio.run(scenario())

    def test_tts_requires_passed_configuration_and_enforces_size(self) -> None:
        async def scenario() -> None:
            missing = MediaService(
                media_settings(
                    azure_speech_key="",
                    azure_speech_region="",
                ),
                openai_client=FakeOpenAI(),
                http_transport=FakeTransport(),
            )
            with self.assertRaisesRegex(
                MediaProviderError,
                "not configured",
            ):
                await missing.synthesize_voice("晚安")

            oversized = MediaService(
                media_settings(max_voice_bytes=3),
                openai_client=FakeOpenAI(),
                http_transport=FakeTransport(
                    HTTPResponse(200, {}, b"four"),
                ),
            )
            with self.assertRaises(MediaTooLargeError):
                await oversized.synthesize_voice("晚安")

        asyncio.run(scenario())

    def test_text_and_image_moderation_use_expected_inputs(self) -> None:
        async def scenario() -> None:
            client = FakeOpenAI(
                moderation_results=(
                    moderation_response(
                        True,
                        {"sexual": True, "violence": False},
                    ),
                    moderation_response(False),
                )
            )
            service = MediaService(
                media_settings(),
                openai_client=client,
                http_transport=FakeTransport(),
            )

            text_decision = await service.moderation_text("測試內容")
            image_decision = await service.moderation_image(
                b"\x89PNG-data",
                "image/png",
            )

            self.assertTrue(text_decision.flagged)
            self.assertFalse(text_decision.allowed)
            self.assertEqual(text_decision.categories, ("sexual",))
            self.assertTrue(image_decision.allowed)
            self.assertEqual(
                client.moderations.calls[0],
                {
                    "model": "omni-moderation-latest",
                    "input": "測試內容",
                },
            )
            image_input = client.moderations.calls[1]["input"]
            data_url = image_input[0]["image_url"]["url"]
            self.assertTrue(data_url.startswith("data:image/png;base64,"))
            self.assertEqual(
                base64.b64decode(data_url.split(",", 1)[1]),
                b"\x89PNG-data",
            )

        asyncio.run(scenario())

    def test_moderation_malformed_response_and_large_image_fail_closed(self) -> None:
        async def scenario() -> None:
            malformed = MediaService(
                media_settings(),
                openai_client=FakeOpenAI(
                    moderation_results=(moderation_response("yes"),)
                ),
                http_transport=FakeTransport(),
            )
            with self.assertRaises(MediaProviderError):
                await malformed.moderation_text("測試")

            limited = MediaService(
                media_settings(max_moderation_image_bytes=2),
                openai_client=FakeOpenAI(),
                http_transport=FakeTransport(),
            )
            with self.assertRaises(MediaTooLargeError):
                await limited.moderation_image(b"123")
            with self.assertRaises(MediaPolicyError):
                await limited.moderation_image(b"1", "image/gif")

        asyncio.run(scenario())

    def test_video_uses_multipart_polls_and_returns_required_preview(self) -> None:
        async def scenario() -> None:
            transport = FakeTransport(
                json_response({"id": "video_123", "status": "queued"}),
                json_response({"id": "video_123", "status": "in_progress"}),
                json_response({"id": "video_123", "status": "completed"}),
                HTTPResponse(
                    200,
                    {"Content-Type": "image/jpeg"},
                    b"spritesheet",
                ),
                HTTPResponse(
                    200,
                    {"Content-Type": "video/mp4"},
                    b"mp4-data",
                ),
            )
            service = MediaService(
                media_settings(),
                openai_client=FakeOpenAI(),
                http_transport=transport,
            )

            artifact = await service.generate_video(
                "海邊散步",
                "週末放鬆",
            )

            self.assertEqual(artifact.kind, MediaKind.VIDEO)
            self.assertEqual(artifact.data, b"mp4-data")
            self.assertEqual(artifact.safety_preview, b"spritesheet")
            self.assertEqual(
                artifact.safety_preview_content_type,
                "image/jpeg",
            )
            self.assertEqual(
                artifact.safety_preview_variant,
                "spritesheet",
            )
            self.assertEqual(artifact.text, "週末放鬆")

            create = transport.calls[0]
            self.assertEqual(create["method"], "POST")
            self.assertEqual(
                create["url"],
                "https://api.openai.test/v1/videos",
            )
            self.assertEqual(
                create["multipart"],
                {"model": "sora-2", "prompt": "海邊散步"},
            )
            self.assertIsNone(create["content"])
            self.assertEqual(
                transport.calls[3]["params"],
                {"variant": "spritesheet"},
            )
            self.assertIsNone(transport.calls[4]["params"])

        asyncio.run(scenario())

    def test_video_preview_falls_back_to_thumbnail(self) -> None:
        async def scenario() -> None:
            transport = FakeTransport(
                json_response({"id": "video_1", "status": "completed"}),
                HTTPResponse(404, {}, b"missing"),
                HTTPResponse(200, {"Content-Type": "image/png"}, b"thumb"),
                HTTPResponse(200, {"Content-Type": "video/mp4"}, b"video"),
            )
            service = MediaService(
                media_settings(),
                openai_client=FakeOpenAI(),
                http_transport=transport,
            )

            artifact = await service.generate_video("海邊")

            self.assertEqual(artifact.safety_preview, b"thumb")
            self.assertEqual(
                artifact.safety_preview_variant,
                "thumbnail",
            )
            self.assertEqual(
                [call["params"] for call in transport.calls[1:3]],
                [
                    {"variant": "spritesheet"},
                    {"variant": "thumbnail"},
                ],
            )

        asyncio.run(scenario())

    def test_video_without_safety_preview_is_never_released(self) -> None:
        async def scenario() -> None:
            transport = FakeTransport(
                json_response({"id": "video_1", "status": "completed"}),
                HTTPResponse(404, {}, b"missing"),
                HTTPResponse(500, {}, b"missing"),
            )
            service = MediaService(
                media_settings(),
                openai_client=FakeOpenAI(),
                http_transport=transport,
            )

            with self.assertRaisesRegex(
                MediaPolicyError,
                "not released",
            ):
                await service.generate_video("海邊")

            self.assertEqual(len(transport.calls), 3)

        asyncio.run(scenario())

    def test_video_timeout_and_explicit_cancellation_are_bounded(self) -> None:
        async def scenario() -> None:
            now = [10.0]

            async def advance(delay: float) -> None:
                now[0] += max(delay, 0.01)
                await asyncio.sleep(0)

            timeout_transport = FakeTransport(
                json_response({"id": "video_1", "status": "queued"}),
            )
            timeout_service = MediaService(
                media_settings(
                    video_timeout_seconds=0.5,
                    video_poll_interval_seconds=1,
                ),
                openai_client=FakeOpenAI(),
                http_transport=timeout_transport,
                clock=lambda: now[0],
                sleep=advance,
            )
            with self.assertRaises(MediaTimeoutError):
                await timeout_service.generate_video("海邊")
            self.assertEqual(len(timeout_transport.calls), 1)

            cancel_event = asyncio.Event()
            cancel_event.set()
            cancelled_transport = FakeTransport()
            cancelled_service = MediaService(
                media_settings(),
                openai_client=FakeOpenAI(),
                http_transport=cancelled_transport,
            )
            with self.assertRaises(MediaCancelledError):
                await cancelled_service.generate_video(
                    "海邊",
                    cancel_event=cancel_event,
                )
            self.assertEqual(cancelled_transport.calls, [])

        asyncio.run(scenario())

    def test_video_can_be_cancelled_while_waiting_to_poll(self) -> None:
        async def scenario() -> None:
            cancel_event = asyncio.Event()
            transport = FakeTransport(
                json_response({"id": "video_1", "status": "queued"}),
            )
            service = MediaService(
                media_settings(video_poll_interval_seconds=60),
                openai_client=FakeOpenAI(),
                http_transport=transport,
            )
            task = asyncio.create_task(
                service.generate_video(
                    "海邊",
                    cancel_event=cancel_event,
                )
            )
            while not transport.calls:
                await asyncio.sleep(0)
            cancel_event.set()

            with self.assertRaises(MediaCancelledError):
                await task

            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(service.queue.pending_jobs, 0)

        asyncio.run(scenario())

    def test_video_failure_invalid_content_and_size_are_rejected(self) -> None:
        async def scenario() -> None:
            failed = MediaService(
                media_settings(),
                openai_client=FakeOpenAI(),
                http_transport=FakeTransport(
                    json_response({"id": "video_1", "status": "failed"}),
                ),
            )
            with self.assertRaisesRegex(
                MediaProviderError,
                "generation failed",
            ):
                await failed.generate_video("海邊")

            too_large = MediaService(
                media_settings(max_video_bytes=3),
                openai_client=FakeOpenAI(),
                http_transport=FakeTransport(
                    json_response(
                        {"id": "video_1", "status": "completed"}
                    ),
                    HTTPResponse(200, {"Content-Type": "image/jpeg"}, b"p"),
                    HTTPResponse(200, {"Content-Type": "video/mp4"}, b"four"),
                ),
            )
            with self.assertRaises(MediaTooLargeError):
                await too_large.generate_video("海邊")

        asyncio.run(scenario())

    def test_text_render_has_no_provider_call_but_still_uses_safety(self) -> None:
        async def scenario() -> None:
            hook = AsyncMock(return_value=True)
            service = MediaService(
                media_settings(),
                safety_gate=MediaSafetyGate(review_hook=hook),
                openai_client=FakeOpenAI(),
                http_transport=FakeTransport(),
            )

            artifact = await service.render(
                MediaIntent(MediaKind.TEXT, text="大家晚安")
            )

            self.assertEqual(artifact.kind, MediaKind.TEXT)
            self.assertIsNone(artifact.data)
            self.assertEqual(artifact.text, "大家晚安")
            hook.assert_awaited_once()

        asyncio.run(scenario())

    def test_render_revalidates_direct_intents_before_safety_or_provider(
        self,
    ) -> None:
        async def scenario() -> None:
            hook = AsyncMock(return_value=True)
            transport = FakeTransport()
            service = MediaService(
                media_settings(),
                safety_gate=MediaSafetyGate(review_hook=hook),
                openai_client=FakeOpenAI(),
                http_transport=transport,
            )

            with self.assertRaises(MediaIntentError):
                await service.render(
                    MediaIntent(MediaKind.VIDEO, prompt="   ")
                )

            hook.assert_not_awaited()
            self.assertEqual(transport.calls, [])

        asyncio.run(scenario())

    def test_settings_read_secrets_only_from_passed_mapping_and_hide_repr(
        self,
    ) -> None:
        settings = MediaSettings.from_env(
            {
                "AI_API_KEY": "must-not-be-used",
                "OPENAI_MEDIA_API_KEY": "primary-secret",
                "MEDIA_OPENAI_API_KEY": "media-secret",
                "OPENAI_MEDIA_BASE_URL": "https://router.example/v1/",
                "MEDIA_OPENAI_BASE_URL": "https://alias.example/v1/",
                "AZURE_SPEECH_KEY": "speech-secret",
                "AZURE_SPEECH_REGION": "EASTASIA",
                "MEDIA_VIDEO_MODEL": "video-model",
            }
        )

        self.assertEqual(settings.openai_api_key, "primary-secret")
        self.assertEqual(
            settings.openai_base_url,
            "https://router.example/v1",
        )
        self.assertEqual(settings.azure_speech_key, "speech-secret")
        self.assertEqual(settings.azure_speech_region, "eastasia")
        self.assertEqual(settings.video_model, "video-model")
        self.assertNotIn("primary-secret", repr(settings))
        self.assertNotIn("speech-secret", repr(settings))

        no_fallback = MediaSettings.from_env(
            {"AI_API_KEY": "text-only-secret"}
        )
        self.assertEqual(no_fallback.openai_api_key, "")


if __name__ == "__main__":
    unittest.main()
