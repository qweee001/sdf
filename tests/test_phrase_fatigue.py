"""詞彙（核心意象）疲勞偵測：emoji 疲勞的姊妹規則。

實測根因（10-15 群 -1002229799107）：三號把「腿軟」當核心意象連用 5 次、
「洗碗」被拖進 2 次——句式都不同、整句去重全部放行，但同一個梗被刷了 5 遍。
整句去重（_recent_proactive_topics）防不住「換了句式、核心詞不變」的變體。
"""

import asyncio
from unittest.mock import AsyncMock

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
        # 跨號單次冷卻會記下各句末核心詞，但不該觸發「同一梗連講」的本地疲勞
        assert worker._fatigued_phrases(group) == []

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


def test_reply_prompt_carries_phrase_hint():
    """回覆路徑也要把詞彙疲勞提示帶進 prompt（先前只接主動）。"""

    async def main():
        worker = _worker(101)
        group = -5565520321
        for text in ("煮到對方腿軟", "想被幹到腿軟", "想舔到我腿軟嗎"):
            worker._record_sent_text(group, text)
        worker._call_ai = AsyncMock(return_value="今天想去吃火鍋")
        event = AsyncMock(
            sender_id=999,
            chat_id=group,
            id=88,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="今晚有人嗎",
            media=None,
        )
        event.sender = None
        assert await worker._generate_reply(event) == "今天想去吃火鍋"
        prompt = worker._call_ai.await_args.args[1]
        assert "腿軟" in prompt

    asyncio.run(main())


def test_generation_rewrites_phrase_fatigue_reply():
    """回覆含疲勞核心詞 → 走既有單次 retry，重生後換意象就放行。"""

    async def main():
        worker = _worker(101)
        group = -5428680940
        for text in ("煮到對方腿軟", "想被幹到腿軟", "想舔到我腿軟嗎"):
            worker._record_sent_text(group, text)
        replies = iter(["還想被幹到腿軟", "今天想去吃火鍋"])
        worker._call_ai = AsyncMock(side_effect=lambda *_a, **_k: next(replies))
        event = AsyncMock(
            sender_id=999,
            chat_id=group,
            id=89,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="今晚有人嗎",
            media=None,
        )
        event.sender = None
        text = await worker._generate_reply(event)
        assert text == "今天想去吃火鍋"
        assert worker._call_ai.await_count == 2
        retry_prompt = worker._call_ai.await_args_list[1].args[1]
        assert "腿軟" in retry_prompt
        assert "核心意象" in retry_prompt

    asyncio.run(main())


def test_generation_drops_reply_when_phrase_fatigue_survives_retry():
    """兩次都還圍著疲勞詞 → 不發送，drop 原因 phrase_fatigue。"""

    async def main():
        worker = _worker(101)
        group = -5428680940
        for text in ("煮到對方腿軟", "想被幹到腿軟", "想舔到我腿軟嗎"):
            worker._record_sent_text(group, text)
        worker._call_ai = AsyncMock(side_effect=lambda *_a, **_k: "還想被幹到腿軟")
        event = AsyncMock(
            sender_id=999,
            chat_id=group,
            id=90,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="今晚有人嗎",
            media=None,
        )
        event.sender = None
        text = await worker._generate_reply(event)
        assert text == ""
        reason = worker._generation_reasons.get(worker._generation_key(event))
        assert reason == "phrase_fatigue"

    asyncio.run(main())


def test_proactive_drops_phrase_fatigue_and_retries():
    """主動生成含疲勞詞 → 既有 3 次 drop 迴圈換一句，換掉就放行。"""

    async def main():
        worker = _worker(101)
        group = -5428680940
        for text in ("煮到對方腿軟", "想被幹到腿軟", "想舔到我腿軟嗎"):
            worker._record_sent_text(group, text)

        class _TopicDB:
            async def get_group_messages(self, group_id, limit=100):
                return [
                    {
                        "sender_name": "風口浪尖",
                        "sender_id": 5,
                        "role": "user",
                        "content": "今晚有人嗎",
                        "timestamp": 1_700_000_000,
                    }
                ]

            async def get_group_shared_notes(self, group_id, account_id, limit=10):
                return []

            async def recent_bot_texts_by_group(self, group_id):
                return []

        worker.db = _TopicDB()
        replies = iter(["還想被幹到腿軟", "今天想去吃火鍋"])
        worker._call_ai = AsyncMock(side_effect=lambda *_a, **_k: next(replies))
        topic = await worker._generate_context_topic(group)
        assert topic == "今天想去吃火鍋"
        assert worker._call_ai.await_count == 2

    asyncio.run(main())


def test_shared_notes_are_background_not_current_topic():
    """shared_notes 標成背景備忘，禁止把舊詞硬湊進當句。"""

    async def main():
        worker = _worker(101)
        group = -5428680940

        class _TopicDB:
            async def get_group_messages(self, group_id, limit=100):
                return [
                    {
                        "sender_name": "風口浪尖",
                        "sender_id": 5,
                        "role": "user",
                        "content": "今晚有人嗎",
                        "timestamp": 1_700_000_000,
                    }
                ]

            async def get_group_shared_notes(self, group_id, account_id, limit=10):
                return ["有人聊過洗碗"]

            async def recent_bot_texts_by_group(self, group_id):
                return []

        worker.db = _TopicDB()
        worker._call_ai = AsyncMock(return_value="今晚想出門晃晃")
        topic = await worker._generate_context_topic(group)
        assert topic == "今晚想出門晃晃"
        prompt = worker._call_ai.await_args.args[1]
        assert "洗碗" in prompt
        assert "背景備忘" in prompt
        assert "硬湊" in prompt
        assert "供接話參考" not in prompt
        assert "要接得上群組當前話題" not in prompt

        worker._call_ai = AsyncMock(return_value="今晚想出門晃晃")
        event = AsyncMock(
            sender_id=999,
            chat_id=group,
            id=91,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="今晚有人嗎",
            media=None,
        )
        event.sender = None
        event._sdf_ctx = {
            "history": [],
            "recent_group_replies": [],
            "shared_notes": ["有人聊過洗碗"],
            "member_notes": [],
            "time_hint": "",
        }
        assert await worker._generate_reply(event) == "今晚想出門晃晃"
        reply_prompt = worker._call_ai.await_args.args[1]
        assert "洗碗" in reply_prompt
        assert "背景備忘" in reply_prompt
        assert "硬湊" in reply_prompt
        assert "供接話參考" not in reply_prompt

    asyncio.run(main())


def test_other_account_avoids_core_within_cooldown():
    """同群任一號用過的核心詞，其他號在冷卻窗內要避開（P1 跨號共享）。"""

    async def main():
        shared: dict = {}
        a = _worker(101)
        b = _worker(202)
        a.phrase_cores_by_group = shared
        b.phrase_cores_by_group = shared
        group = -1001
        a._record_sent_text(group, "煮到對方腿軟")
        assert "腿軟" in b._phrase_fatigue_hint(group)
        assert b._phrase_fatigue_hit(group, "想舔到我腿軟嗎") == "腿軟"
        # 講過的那隻自己也別立刻換句式再抬一次
        assert a._phrase_fatigue_hit(group, "想被幹到腿軟") == "腿軟"

    asyncio.run(main())


def test_shared_core_expires_after_cooldown():
    """跨號核心詞冷卻到期後不再硬擋。"""
    import time

    async def main():
        shared: dict = {}
        a = _worker(101)
        b = _worker(202)
        a.phrase_cores_by_group = shared
        b.phrase_cores_by_group = shared
        group = -1001
        a._record_sent_text(group, "煮到對方腿軟")
        assert b._phrase_fatigue_hit(group, "想舔到我腿軟嗎") == "腿軟"
        shared[group][:] = [
            (core, ts - 46 * 60) for core, ts in shared[group]
        ]
        assert time.time() - shared[group][0][1] > 45 * 60
        assert b._phrase_fatigue_hit(group, "想舔到我腿軟嗎") == ""
        assert "腿軟" not in (b._phrase_fatigue_hint(group) or "")

    asyncio.run(main())


def test_shared_core_isolated_per_group():
    """跨號冷卻也按群組隔離。"""

    async def main():
        shared: dict = {}
        a = _worker(101)
        b = _worker(202)
        a.phrase_cores_by_group = shared
        b.phrase_cores_by_group = shared
        a._record_sent_text(-1001, "煮到對方腿軟")
        assert b._phrase_fatigue_hit(-2002, "想舔到我腿軟嗎") == ""
        assert b._phrase_fatigue_hint(-2002) == ""

    asyncio.run(main())


def test_shared_core_ignores_common_function_words():
    """今天／真的／可以 這類功能詞不該跨號冷卻。"""

    async def main():
        shared: dict = {}
        a = _worker(101)
        b = _worker(202)
        a.phrase_cores_by_group = shared
        b.phrase_cores_by_group = shared
        group = -1001
        a._record_sent_text(group, "今天真的可以")
        assert b._phrase_fatigue_hit(group, "今天想出門晃晃") == ""
        cores = [core for core, _ts in shared.get(group, [])]
        assert "今天" not in cores
        assert "真的" not in cores
        assert "可以" not in cores

    asyncio.run(main())


def test_manager_injects_shared_phrase_cores():
    """manager 要把同一份 phrase_cores_by_group 注入所有 worker。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from app.manager import AccountManager

    async def main():
        manager = AccountManager.__new__(AccountManager)
        manager.config = SimpleNamespace(tg_api_id=1, tg_api_hash="hash")
        manager.db = SimpleNamespace(
            update_account=AsyncMock(),
            list_accounts=AsyncMock(return_value=[]),
            last_human_activity_by_group=AsyncMock(return_value={}),
        )
        manager.secret_box = SimpleNamespace(decrypt=lambda value: value)
        manager.workers = {}
        manager._ai_client = None
        manager._chat_client = None
        manager._media_service = None
        manager._voice_library = SimpleNamespace()
        manager.managed_ids = set()
        manager.active_ids = set()
        manager.active_group_ids = {}
        manager.managed_origins = {}
        manager.human_owners = {}
        manager.recent_proactive_owners = {}
        manager.last_human_activity = {}
        manager.reply_claim_signals = {}
        manager.failed_reply_claimants = {}
        manager.topic_turn_counts = {}
        manager.personas = {}
        manager.phrase_cores_by_group = {}
        manager.live_test = SimpleNamespace(
            start_block_error=AsyncMock(return_value=""),
            outbound_gate=None,
        )
        captured = []

        class FakeWorker:
            def __init__(self, **kwargs):
                captured.append(kwargs)
                self.is_running = True
                self.start = AsyncMock()

        with patch("app.manager.AccountWorker", FakeWorker):
            await manager._start_account({
                "id": "worker-1",
                "session_key": "encrypted-session",
                "groups": "[-1001]",
            })

        assert captured
        assert captured[0]["phrase_cores_by_group"] is manager.phrase_cores_by_group

    asyncio.run(main())
