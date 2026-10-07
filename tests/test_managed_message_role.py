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
                          message_id=0):
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
