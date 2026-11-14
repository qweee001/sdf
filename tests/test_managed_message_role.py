"""水軍同伴的訊息必須記成 assistant，不能記成 user。

實測（群 111）：只有發話者自己那份記 assistant，別人收到時一律記 user，
導致「真人活動」判定、純水軍串剎車、輪替、控制台真人數全部被水軍訊息灌水。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from test_worker_reply_arbitration import MANAGED, _ClaimDB, _worker

GROUP = -5428680940
PEER = sorted(MANAGED)[1]


class _RecordingDB(_ClaimDB):
    def __init__(self):
        super().__init__()
        self.recorded = []
        self.events = []

    async def add_message(self, account_id, group_id, sender_id, sender_name, role, content,
                          message_id=0, sent_at=None):
        self.recorded.append(
            {
                "account_id": account_id,
                "group_id": group_id,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "role": role,
                "content": content,
            }
        )

    async def record_group_event(self, *args, **kwargs):
        self.events.append((args, kwargs))

    async def get_group_shared_notes(self, *_a, **_k):
        return []

    async def get_group_member_notes(self, *_a, **_k):
        return []


def _incoming(sender_id, text="先練練舌頭功"):
    event = MagicMock()
    event.is_private = False
    event.is_group = True
    event.chat_id = GROUP
    event.sender_id = sender_id
    event.raw_text = text
    event.mentioned = False
    event.is_reply = False
    event.reply_to = None
    event.media = None
    event.user_joined = False
    event.get_sender = AsyncMock(return_value=SimpleNamespace(first_name="同伴", last_name="", username="peer"))
    return event


def test_peer_water_army_message_is_recorded_as_assistant():
    async def main():
        db = _RecordingDB()
        worker = _worker(sorted(MANAGED)[0], db=db)
        worker.tg_user_id = sorted(MANAGED)[0]
        worker.is_running = True
        worker.tg_client = object()
        worker._should_reply = AsyncMock(return_value=False)
        await worker.on_message(_incoming(PEER))
        assert db.recorded, "訊息應該要被記錄"
        assert db.recorded[0]["role"] == "assistant", db.recorded[0]
        assert db.recorded[0]["sender_id"] == PEER

    asyncio.run(main())


def test_real_human_message_stays_user():
    async def main():
        db = _RecordingDB()
        worker = _worker(sorted(MANAGED)[0], db=db)
        worker.tg_user_id = sorted(MANAGED)[0]
        worker.is_running = True
        worker.tg_client = object()
        worker._should_reply = AsyncMock(return_value=False)
        await worker.on_message(_incoming(900123, "有人在嗎"))
        assert db.recorded[0]["role"] == "user", db.recorded[0]

    asyncio.run(main())


def test_should_reply_binds_group_id_for_peer_and_human():
    """`_should_reply` 自己在姊妹連發／非點名兩處先用到 group_id。

    這兩處都在它自己的賦值點（原 2405 行）之前，等於每一則訊息都 UnboundLocalError
    → on_message 整段中止：回覆全死、只剩主動發言。上面的測試把 `_should_reply`
    整個 mock 掉，正是漏掉這條的原因，所以這裡打真的。
    """

    async def main():
        me = sorted(MANAGED)[0]
        for sender in (PEER, 900123):
            worker = _worker(me, db=_RecordingDB())
            worker.tg_user_id = me
            worker.reply_enabled = True
            worker.persona = {"name": "小小"}
            worker._own_streak = AsyncMock(return_value=2)
            event = _incoming(sender)
            event.is_bot = False
            assert await worker._should_reply(event) is False
            worker._own_streak.assert_awaited_once_with(GROUP)

    asyncio.run(main())
