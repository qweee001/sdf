"""貼圖＝明確遞話：照片不該跟普通閒聊同一道機率。

11-08 16:52:27 輕輕貼一張猜煮什麼的圖，四號全數跳過：images_seen 有記、
回覆零、連識圖都沒跑（images_understood 0）。照片落在 ordinary 路徑，
要同時過 base 機率、90 秒扎堆讓位與熱場額度三道，三道都是為「文字閒聊太密」
設計的，對「有人丟東西給大家看」反而過嚴。
"""

import asyncio
from types import SimpleNamespace

from telethon.tl.types import MessageMediaPhoto, PhotoEmpty

from test_worker_reply_arbitration import MANAGED, _event, _worker  # noqa: E402


def _photo_event(**kwargs):
    event = _event(**kwargs)
    event.media = MessageMediaPhoto(PhotoEmpty(id=1))
    event.file = SimpleNamespace(size=1234, mime_type="image/jpeg")
    return event


def test_photo_is_answered_even_with_zero_base_probability():
    async def main():
        db = type("D", (), {})()
        worker = _worker(sorted(MANAGED)[0], base_reply_probability=0.0)
        allowed = await worker._ordinary_reply_allowed(_photo_event())
        assert allowed is True

    asyncio.run(main())


def test_plain_text_still_loses_with_zero_base_probability():
    async def main():
        worker = _worker(sorted(MANAGED)[0], base_reply_probability=0.0)
        assert await worker._ordinary_reply_allowed(_event()) is False

    asyncio.run(main())


def test_photo_burst_still_claims_only_one_account():
    """放寬的是機率，不是排他：四號同擲仍只有一個拿到 claim。"""

    async def main():
        from test_worker_reply_arbitration import _ClaimDB

        event = _photo_event()
        db = _ClaimDB()
        decisions = [
            await _worker(uid, db=db)._should_reply(event) for uid in sorted(MANAGED)
        ]
        assert decisions.count(True) == 1

    asyncio.run(main())
