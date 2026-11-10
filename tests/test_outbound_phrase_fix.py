"""固定說法校正必須在唯一出口生效，兩條路徑都吃。

實測 16:00 Chen 主動發言送出「找助理了解喔」，但群裡真人講的是「聯絡助理」。
原因是 fix_phrases 只接在回覆路徑（_reply_later），主動路徑完全沒有。
出口只有一個（_send_message_unlocked），校正放這裡才不會漏下一條路徑。
"""

import asyncio
from unittest.mock import AsyncMock

from test_worker_reply_arbitration import _worker

GROUP = -1001


def _ready(worker):
    worker.is_running = True
    worker.selected_groups = [GROUP]
    worker._activity_enabled = lambda *_a, **_k: True
    worker._fixed_persona_integrity_ok = AsyncMock(return_value=True)
    worker.outbound_gate = None
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))
        return SimpleMessage()

    class SimpleMessage:
        id = 1

    worker.tg_client = type("C", (), {"send_message": staticmethod(_send)})()
    worker.db.add_message = AsyncMock()
    worker.db.set_last_agent_at = AsyncMock()
    return sent


def test_outbound_fixes_zhao_zhuli(monkeypatch):
    async def main():
        worker = _worker(101)
        sent = _ready(worker)
        monkeypatch.setattr("app.worker.asyncio.sleep", AsyncMock())
        await worker._send_message_unlocked(GROUP, "找助理了解喔")
        body = sent[0][0][1]
        assert body == "聯絡助理了解喔", body

    asyncio.run(main())


def test_outbound_keeps_contact_assistant_intact(monkeypatch):
    async def main():
        worker = _worker(101)
        sent = _ready(worker)
        monkeypatch.setattr("app.worker.asyncio.sleep", AsyncMock())
        await worker._send_message_unlocked(GROUP, "去聯絡助理排時間")
        assert sent[0][0][1] == "去聯絡助理排時間"

    asyncio.run(main())
