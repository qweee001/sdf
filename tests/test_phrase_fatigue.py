"""詞彙（核心意象）疲勞偵測：emoji 疲勞的姊妹規則。

實測根因（10-15 群 -1002229799107）：三號把「腿軟」當核心意象連用 5 次、
「洗碗」被拖進 2 次——句式都不同、整句去重全部放行，但同一個梗被刷了 5 遍。
整句去重（_recent_proactive_topics）防不住「換了句式、核心詞不變」的變體。
"""

import asyncio

from test_worker_reply_arbitration import _worker


def test_phrase_fatigue_hint_after_core_word_repeats():
    """同一個短詞當核心意象反覆出現時，提示模型換意象。"""

    async def main():
        worker = _worker(101)
        group = -5565520321
        # 模擬「腿軟」被當核心意象連用（句式不同、核心詞相同）
        for text in (
            "煮到對方腿軟",
            "想被幹到腿軟",
            "想舔到我腿軟嗎",
        ):
            worker._record_sent_text(group, text)
        hint = worker._phrase_fatigue_hint(group)
        assert "腿軟" in hint, f"核心詞重複要給提示: {hint!r}"

    asyncio.run(main())


def test_phrase_fatigue_hint_absent_when_words_vary():
    """核心詞各不相同時不給提示（正常多句不誤傷）。"""

    async def main():
        worker = _worker(101)
        group = -5565520321
        for text in (
            "今天想吃牛肉麵",
            "週末想去爬山",
            "最近在看一部新劇",
        ):
            worker._record_sent_text(group, text)
        assert worker._phrase_fatigue_hint(group) == ""

    asyncio.run(main())


def test_phrase_fatigue_hint_isolated_per_group():
    """詞彙疲勞按群組隔離，A 群的梗不影響 B 群。"""

    async def main():
        worker = _worker(101)
        for _ in range(3):
            worker._record_sent_text(-1001, "煮到腿軟")
        assert "腿軟" in worker._phrase_fatigue_hint(-1001)
        assert worker._phrase_fatigue_hint(-2002) == ""

    asyncio.run(main())


def test_phrase_history_is_capped():
    """歷史句子只留最近 N 條（不無限成長）。"""

    async def main():
        worker = _worker(101)
        group = -1001
        for i in range(20):
            worker._record_sent_text(group, f"訊息{i}")
        assert len(worker._recent_texts_by_group[group]) <= 8

    asyncio.run(main())


def test_generation_prompt_carries_phrase_hint():
    """主動話題生成時要把詞彙疲勞提示帶進 prompt。"""

    async def main():
        from unittest.mock import AsyncMock

        worker = _worker(101)
        group = -5428680940
        # 先製造「腿軟」核心意象重複
        for text in ("煮到對方腿軟", "想被幹到腿軟", "想舔到我腿軟嗎"):
            worker._record_sent_text(group, text)

        class _TopicDB:
            def __init__(self):
                self.recent_group_messages = [
                    {"sender_name": "風口浪尖", "text": "今天煮到腿軟", "kind": "human"},
                ]

            async def get_group_recent_messages(self, group_id, *, hours=48, limit=80):
                return self.recent_group_messages

            async def get_group_messages(self, group_id, limit=100):
                return []

            async def get_group_shared_notes(self, group_id, account_id, limit=10):
                return []

        worker.db = _TopicDB()
        prompts = []
        worker._call_ai = AsyncMock(
            side_effect=lambda s, p, **kw: (prompts.append(p), "今天想去吃火鍋")[1]
        )
        fresh = await worker._generate_context_topic(group)
        assert fresh == "今天想去吃火鍋"
        assert prompts, "至少呼叫一次模型"
        assert "腿軟" in prompts[0], f"核心意象重複要餵進 prompt: {prompts[0][-300:]}"

    asyncio.run(main())
