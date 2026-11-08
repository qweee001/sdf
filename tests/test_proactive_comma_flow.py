"""兩段式骨架的動態斷句：連續四則都靠逗號撐著，這則就斷成單句。"""

import asyncio
import time
from unittest.mock import AsyncMock

from app import worker as worker_mod
from test_proactive_dedup import _TopicDB
from test_worker_reply_arbitration import _worker

import pytest


@pytest.fixture(autouse=True)
def _fixed_roll(monkeypatch):
    """固定骰子（0.9＝scene 帶）：不然抽到短碎帶時，未觸發逗號剪枝的對照組
    也會被字數帶剪掉，比不出這條規則的效果。"""
    monkeypatch.setattr(worker_mod.random, "random", lambda: 0.9)

GROUP = -5428680940
COMMA_RUN = [
    "戰豆，慾望不強怎麼把你吸乾呀",
    "請教這麼乖，偷跑出來喝酒敢嗎",
    "萬英尺，我正在脫高跟鞋揉小腿",
    "天天開心，直接拿你的雞雞來試我深淺",
    "和進出口，包你全身發燙",
]


def _seed(worker):
    worker._recent_texts_by_group[GROUP] = list(COMMA_RUN)
    worker._recent_texts_at[GROUP] = [time.time()] * 5


def test_comma_run_breaks_the_two_clause_mold():
    async def main():
        worker = _worker(202, db=_TopicDB())
        _seed(worker)
        assert worker._comma_flow(GROUP) == 5
        worker._call_ai = AsyncMock(return_value="請教連在家喝酒都被管喔，我正咬著吸管喝手搖")
        topic = await worker._generate_context_topic(GROUP)
        assert topic == "我正咬著吸管喝手搖"
        assert worker.stats["strip_comma_flow"] == 1

    asyncio.run(main())


def test_no_comma_run_keeps_the_whole_line():
    async def main():
        worker = _worker(202, db=_TopicDB())
        worker._recent_texts_by_group[GROUP] = ["剛擦乳液", "腿好痠", "好累喔", "先睡了", "明天見"]
        worker._recent_texts_at[GROUP] = [0.0] * 5
        assert worker._comma_flow(GROUP) == 0
        worker._call_ai = AsyncMock(return_value="請教連在家喝酒都被管喔，我正咬著吸管喝手搖")
        assert await worker._generate_context_topic(GROUP) == (
            "請教連在家喝酒都被管喔，我正咬著吸管喝手搖"
        )
        assert "strip_comma_flow" not in worker.stats

    asyncio.run(main())


def test_short_tail_is_left_alone():
    """後半句短於 8 字就不剪，否則會出現「腿好痠喔」這種沒頭沒尾的殘句。"""

    async def main():
        worker = _worker(202, db=_TopicDB())
        _seed(worker)
        worker._call_ai = AsyncMock(return_value="萬英尺，腿好痠喔")
        assert await worker._generate_context_topic(GROUP) == "萬英尺，腿好痠喔"

    asyncio.run(main())
