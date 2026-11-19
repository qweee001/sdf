"""看圖入口：大小未知不准當成太大擋掉。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telethon.tl.types import MessageMediaPhoto

from app.worker import AccountWorker


class Stub:
    def __init__(self, max_bytes=1000):
        self.config = SimpleNamespace(media_enabled=True, media_max_input_bytes=max_bytes)
        self.media_service = object()
        self.stats: dict = {}

    def _img_reject(self, reason: str) -> None:
        key = f"img_none_{reason}"
        self.stats[key] = int(self.stats.get(key, 0)) + 1


def _event(size, payload=b"x" * 10):
    async def download_media(file=bytes):
        return payload

    return SimpleNamespace(media=MessageMediaPhoto(), file=SimpleNamespace(size=size, mime_type="image/jpeg"), download_media=download_media)


def test_unknown_size_still_downloads():
    """file.size 是 0／None 时，以前一律判 bad_size，等于整批照片被挡在门外。"""
    for size in (0, None):
        stub = Stub()
        out = asyncio.run(AccountWorker._incoming_image(stub, _event(size)))
        assert out is not None
        assert stub.stats.get("img_fetch_ok") == 1
        assert "img_none_bad_size" not in stub.stats


def test_known_oversize_skips_download():
    stub = Stub(max_bytes=5)
    out = asyncio.run(AccountWorker._incoming_image(stub, _event(999)))
    assert out is None
    assert stub.stats.get("img_none_too_big_known") == 1


def test_oversize_after_download_is_rejected():
    """宣告大小没过，但实际下载回来更大：仍要在下载后挡。"""
    stub = Stub(max_bytes=50)
    out = asyncio.run(AccountWorker._incoming_image(stub, _event(10, b"x" * 200)))
    assert out is None
    assert stub.stats.get("img_none_too_big") == 1


def test_disabled_media_is_counted():
    stub = Stub()
    stub.config.media_enabled = False
    out = asyncio.run(AccountWorker._incoming_image(stub, _event(10)))
    assert out is None
    assert stub.stats.get("img_none_no_service") == 1
