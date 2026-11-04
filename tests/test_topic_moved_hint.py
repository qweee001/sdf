"""回覆遲到、群裡早翻頁時的修正指令（2026-11-03 21:35:38 / 21:41:06 實測破口）。

Chen 兩次都在目標訊息後 66 秒才回，接的是早被三則新話淹掉的舊訊息：
21:34:32「需要我幫忙嗎」→ 21:35:38「幫忙？先讓我看看你有幾兩重再說」
（此時群裡已經換到琪琪掛毛）；
21:40:00「變黑我也可以舔粉」→ 21:41:06「舔粉是什麼新招」
（風口浪尖、你說會喜歡我、深情森森已經連問三則同型問題，Chen 是第四手重問）。

修法：命中「目標訊息 >45 秒、後面又冒出 ≥3 則真人發言」就把當前熱點併進指令，
要求跟上現在的句子、用斷言收、不准重問已問過/已答過的同一件事。
被點名、姊妹互撩（話題軸線同一條）不套用。
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast

from app.database import Database
from app.worker import AccountWorker


def _worker(name: str = "Chen") -> AccountWorker:
    config = SimpleNamespace(
        ai_model="test-model",
        ai_temperature=0.8,
        ai_max_tokens=200,
        ai_timeout=17,
        ai_disable_thinking=True,
        memory_max_messages=10,
        min_typing_delay=0,
        max_typing_delay=0,
        media_enabled=True,
        media_max_input_bytes=8 * 1024 * 1024,
        decision_enabled=False,
    )
    w = AccountWorker(
        account_id="w1",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=cast(Any, SimpleNamespace()),
        config=config,
        managed_ids={999},
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[-1001],
    )
    w.persona = {"name": name, "gender": "女", "age": 38}
    w.tg_user_id = 101
    return w


def _event(group_id: int = -1001, sender_id: int = 42, gap: float = 66.0, mentioned: bool = False):
    return SimpleNamespace(
        chat_id=group_id,
        sender_id=sender_id,
        date=time.time() - gap,
        raw_text="變黑我也可以舔粉",
        mentioned=mentioned,
        _sdf_seen_at=time.time() - gap,
    )


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows

    async def get_group_messages(self, group_id, limit=10):
        return self.rows[-limit:]


def _rows(sent_at: float, count: int):
    rows = [{"role": "user", "content": "變黑我也可以舔粉", "timestamp": sent_at}]
    for i in range(count):
        rows.append(
            {
                "role": "user",
                "content": f"第{i}則後續：舌頭有毛病／我雞雞13公分／沒有試過20公分",
                "timestamp": sent_at + 2 + i * 3,
            }
        )
    return rows


def _run(coro):
    return asyncio.run(coro)


def _hint(w, event, rows, directed=False):
    w.db = cast(Any, _FakeDb(rows))
    reply_message = SimpleNamespace(sender_id=101) if directed else None
    return _run(w._topic_moved_hint(event, reply_message))


def test_hint_when_thread_moved_on():
    w = _worker()
    ev = _event(gap=66)
    sent_at = ev.date
    hint = _hint(w, ev, _rows(sent_at, 3))
    assert "話題已翻頁" in hint
    assert "第2則後續" in hint


def test_silent_when_only_two_newer_messages():
    w = _worker()
    ev = _event(gap=66)
    assert _hint(w, ev, _rows(ev.date, 2)) == ""


def test_silent_when_target_is_still_fresh():
    w = _worker()
    ev = _event(gap=20)
    assert _hint(w, ev, _rows(ev.date, 5)) == ""


def test_silent_when_named_directly():
    w = _worker()
    ev = _event(gap=90)
    assert _hint(w, ev, _rows(ev.date, 5), directed=True) == ""


def test_silent_when_mentioned():
    w = _worker()
    ev = _event(gap=90, mentioned=True)
    assert _hint(w, ev, _rows(ev.date, 5)) == ""


def test_silent_for_sister_followup():
    w = _worker()
    ev = _event(gap=90, sender_id=999)  # 同伴帳號
    assert _hint(w, ev, _rows(ev.date, 5)) == ""


def test_last_bot_ts_by_group_reads_latest_bot_message():
    async def scenario():
        db = Database(":memory:")
        await db.connect()
        now = time.time()
        await db.add_message("w1", -1001, 42, "男", "user", "真人說話")
        await db.add_message("w1", -1001, 7, "小小", "assistant", "水軍一")
        await asyncio.sleep(0.01)
        await db.add_message("w1", -1001, 8, "Chen", "assistant", "水軍二")
        try:
            return await db.last_bot_ts_by_group(-1001), await db.last_bot_ts_by_group(-2002)
        finally:
            await db.close()

    newest, empty = _run(scenario())
    assert newest > time.time() - 60
    assert empty == 0.0
