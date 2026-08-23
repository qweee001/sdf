import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.media import OrcaMediaService


class _DB:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = []

    async def reserve_media_budget(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.allowed


class _Binary:
    def __init__(self, data):
        self._data = data

    async def aread(self):
        return self._data


class _HTTPResponse:
    def __init__(self, payload=None, content=b"", status=200):
        self._payload = payload
        self.content = content
        self.status_code = status
        self.headers = {
            "content-type": "video/mp4",
            "Content-Length": str(len(content)) if content else "",
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload

    async def aiter_bytes(self):
        midpoint = max(1, len(self.content) // 2)
        yield self.content[:midpoint]
        if self.content[midpoint:]:
            yield self.content[midpoint:]


class _StreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class _HTTP:
    def __init__(self):
        self.post = AsyncMock(return_value=_HTTPResponse({"task_id": "task-1"}))
        self.get = AsyncMock(side_effect=[
            _HTTPResponse({"data": {"status": "IN_PROGRESS", "task_id": "task-1"}}),
            _HTTPResponse({"data": {"status": "SUCCESS", "task_id": "task-1", "result_url": "https://media.test/out.mp4"}}),
        ])
        self.stream_calls = []

    def stream(self, method, url):
        self.stream_calls.append((method, url))
        return _StreamContext(_HTTPResponse(content=b"mp4-bytes"))


async def _public_resolver(_host, _port):
    return ["8.8.8.8"]


def _service(db=None, http=None, resolver=None):
    image_response = SimpleNamespace(data=[SimpleNamespace(
        b64_json=base64.b64encode(b"png-bytes").decode(), url=None
    )])
    message = SimpleNamespace(content="照片裡是一杯咖啡")
    chat_response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    client = SimpleNamespace(
        images=SimpleNamespace(generate=AsyncMock(return_value=image_response)),
        audio=SimpleNamespace(
            speech=SimpleNamespace(create=AsyncMock(return_value=_Binary(b"opus-bytes")))
        ),
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=chat_response))
        ),
    )
    config = SimpleNamespace(
        vision_model="obsidian/Qwen3.8-27B",
        image_model="google/imagen-4.0-fast-generate-001",
        speech_model="openai/tts-1",
        video_model="minimax/minimax-h3",
        media_daily_budget_usd=10.0,
        media_generation_timeout=30.0,
        media_download_hosts=("media.test",),
        ai_api_key="x",
        ai_base_url="https://api.orcarouter.ai/v1",
        ai_temperature=0.7,
        ai_max_tokens=512,
        ai_timeout=60.0,
    )
    service = OrcaMediaService(
        client=client,
        db=db or _DB(),
        config=config,
        http_client=http or _HTTP(),
        resolve_host=resolver or _public_resolver,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    return service, client


def test_image_understanding_uses_local_data_url():
    async def main():
        db = _DB()
        service, client = _service(db=db)
        text = await service.understand_image(
            "a1", b"jpeg", "image/jpeg", "只描述圖片", "使用者傳來的照片"
        )
        assert text == "照片裡是一杯咖啡"
        assert db.calls[0][0][1:3] == ("vision", 0.10)
        kwargs = client.chat.completions.create.await_args.kwargs
        parts = kwargs["messages"][1]["content"]
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    asyncio.run(main())


def test_image_generation_reserves_budget_and_forces_non_explicit_adult_boundary():
    async def main():
        db = _DB()
        service, client = _service(db=db)
        asset = await service.generate_image("a1", "自拍，性感睡衣")
        assert asset and asset.kind == "image" and asset.data == b"png-bytes"
        assert db.calls[0][0][1:3] == ("image", 0.03)
        prompt = client.images.generate.await_args.kwargs["prompt"]
        assert "fictional adult age 21+" in prompt
        assert "no visible genitals" in prompt

    asyncio.run(main())


def test_budget_denial_stops_before_image_api_call():
    async def main():
        service, client = _service(db=_DB(allowed=False))
        assert await service.generate_image("a1", "自拍") is None
        client.images.generate.assert_not_awaited()

    asyncio.run(main())


def test_unapproved_model_is_rejected_before_budget_or_api_call():
    async def main():
        db = _DB()
        service, client = _service(db=db)
        service.config.image_model = "unknown/expensive-image-model"
        with pytest.raises(ValueError, match="未核准"):
            await service.generate_image("a1", "自拍")
        assert db.calls == []
        client.images.generate.assert_not_awaited()

    asyncio.run(main())


def test_voice_generation_returns_telegram_opus():
    async def main():
        service, _client = _service()
        asset = await service.generate_voice("a1", "今晚想你陪我")
        assert asset and asset.kind == "voice"
        assert asset.filename.endswith(".ogg")
        assert asset.data == b"opus-bytes"

    asyncio.run(main())


def test_video_generation_submits_once_polls_same_task_and_downloads():
    async def main():
        http = _HTTP()
        service, _client = _service(http=http)
        asset = await service.generate_video("a1", "穿著睡衣在房間揮手")
        assert asset and asset.kind == "video" and asset.data == b"mp4-bytes"
        assert http.post.await_count == 1
        assert all("task-1" in call.args[0] for call in http.get.await_args_list[:2])
        assert http.stream_calls == [("GET", "https://media.test/out.mp4")]
        submitted = http.post.await_args.kwargs["json"]
        assert submitted["duration"] == 4
        assert "no nudity" in submitted["prompt"]

    asyncio.run(main())


def test_download_rejects_private_ip_before_http_request():
    async def main():
        http = _HTTP()
        service, _client = _service(http=http)
        with pytest.raises(ValueError, match="不安全"):
            await service._download("https://127.0.0.1/internal")
        assert http.stream_calls == []

    asyncio.run(main())


def test_download_rejects_allowed_host_resolving_to_private_ip():
    async def private_resolver(_host, _port):
        return ["127.0.0.1"]

    async def main():
        http = _HTTP()
        service, _client = _service(http=http, resolver=private_resolver)
        with pytest.raises(ValueError, match="不安全"):
            await service._download("https://media.test/internal")
        assert http.stream_calls == []

    asyncio.run(main())
