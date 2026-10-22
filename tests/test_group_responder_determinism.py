"""群聊「誰該接話」必須對所有 worker 給出同一個答案。

回歸測試：舊版 `_pick_group_responder` 只排除「自己」是剛發言者的情況，
於是四個 worker 對同一則訊息算出不同 winner（非最後發言者回傳
last_water_sender、最後發言者回傳雜湊抽選），誰先 claim 誰贏，
等於沒有排程，還會出現同一個機器人連著講兩句。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.worker import AccountWorker
from tests.test_worker_reply_arbitration import _ClaimDB, _worker

GROUP_ID = -1002229799107
CANDIDATES = (101, 202, 303, 404)


def _event(message_id: int = 9001, text: str = "今天好累喔") -> SimpleNamespace:
    return SimpleNamespace(
        chat_id=GROUP_ID,
        id=message_id,
        message=SimpleNamespace(id=message_id, entities=[], reply_to=None),
        raw_text=text,
        is_reply=False,
        mentioned=False,
        media=None,
        sender_id=999,
    )


def _db_with(last_water_sender: int | None) -> _ClaimDB:
    db = _ClaimDB()
    rows = [
        {"role": "user", "sender_id": 999, "content": "有人今天也加班嗎"},
        {"role": "assistant", "sender_id": 101, "content": "我剛下班"},
    ]
    if last_water_sender is not None:
        rows.append(
            {
                "role": "assistant",
                "sender_id": last_water_sender,
                "content": "累到不想動",
            }
        )
    db.get_group_messages = AsyncMock(return_value=rows)
    return db


def _workers(db, base_reply_probability: float = 1.0) -> list[AccountWorker]:
    return [
        _worker(
            uid,
            active_ids=set(CANDIDATES),
            db=db,
            base_reply_probability=base_reply_probability,
            active_group_ids={GROUP_ID: set(CANDIDATES)},
            selected_groups={GROUP_ID},
        )
        for uid in CANDIDATES
    ]


def test_all_workers_agree_on_the_same_responder():
    async def main():
        db = _db_with(last_water_sender=202)
        event = _event()
        winners = set()
        for worker in _workers(db):
            winners.add(await worker._pick_group_responder(event))
        # 四個 worker 各算一票，答案必須完全一致（可重現、可預測）
        assert len(winners) == 1, f"排程結果不一致：{winners}"
        assert winners != {0}, "有候選人卻選不出任何人"

    asyncio.run(main())


def test_last_speaker_never_speaks_twice_in_a_row():
    async def main():
        db = _db_with(last_water_sender=202)
        event = _event()
        for worker in _workers(db):
            winner = await worker._pick_group_responder(event)
            assert winner != 202, "剛發過言的帳號又接了一句（一眼就假）"

    asyncio.run(main())


def test_silent_account_gets_the_turn_before_recent_speakers():
    async def main():
        # 101 剛講過，202 更早講過，303/404 完全沒講過 → 應該從沒講過的裡面挑
        db = _ClaimDB()
        db.get_group_messages = AsyncMock(
            return_value=[
                {"role": "assistant", "sender_id": 202, "content": "早"},
                {"role": "assistant", "sender_id": 101, "content": "剛到公司"},
            ]
        )
        event = _event()
        winner = await _workers(db)[0]._pick_group_responder(event)
        assert winner in {303, 404}, f"沒輪到沉默最久的人，卻選了 {winner}"

    asyncio.run(main())


def test_responder_is_stable_for_the_same_message():
    async def main():
        db = _db_with(last_water_sender=202)
        event = _event(message_id=7777)
        first = await _workers(db)[0]._pick_group_responder(event)
        second = await _workers(db)[0]._pick_group_responder(event)
        assert first == second

    asyncio.run(main())
