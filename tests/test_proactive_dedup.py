"""去重持久化與跨帳號撞句攔截（沒有預設池之後，去重全壓在即時生成的話題上）。

- reload: proactive 去重集合跨帳號共享並持久化；重啟後從 DB 回填。
- 生成: 同一群其他帳號講過的句子，本帳號不得再發；撞句就回空字串（寧可不發）。
"""

import asyncio

from test_worker_reply_arbitration import _ClaimDB, _worker


class _TopicDB(_ClaimDB):
    """提供即時話題生成需要的讀取介面。"""

    def __init__(self, history=(), msgs=()):
        super().__init__()
        self.history = list(history)
        self.msgs = list(msgs)

    async def recent_bot_texts_by_group(self, group_id, *, hours=48, limit=200):
        return list(self.history)

    async def get_group_messages(self, group_id, limit=100):
        return list(self.msgs)

    async def get_group_shared_notes(self, group_id, account_id, limit=10):
        return []


def test_worker_backfills_recent_proactive_topics_from_db():
    """重啟後 worker 必須從 DB 回填最近已發話題（含其他帳號發的），否則重啟清零必然復讀。"""

    async def main():
        seen = [
            "附近有人嗎？先聊得來再決定要不要見",
            "週末想去逛書店，有人也喜歡嗎？",
        ]

        class BackfillDB(_ClaimDB):
            async def recent_bot_texts_by_group(self, group_id, *, hours=48, limit=200):
                return list(seen)

        worker = _worker(101, db=BackfillDB())
        await worker.reload_proactive_memory()
        normalized = worker._recent_proactive_topics
        assert len(normalized) == 2, normalized
        assert any("附近有人嗎" in t for t in normalized)
        assert any("週末想去逛書店" in t for t in normalized)

    asyncio.run(main())


def test_context_topic_skips_text_another_account_already_sent():
    """同一群內其他帳號講過的句子，即時生成時必須被攔下（回空字串，不塞罐頭句）。"""

    async def main():
        from unittest.mock import AsyncMock

        already = "今天超有精神，有人想出去晃晃嗎？"
        worker = _worker(202, db=_TopicDB(history=[already]))
        await worker.reload_proactive_memory()
        assert worker._normalized_reply(already) in worker._recent_proactive_topics

        # 模型吐出同一句 → 拒絕（跨帳號去重）
        worker._call_ai = AsyncMock(return_value=already)
        assert await worker._generate_context_topic(-5428680940) == ""

        # 模型換一句新的 → 放行
        worker._call_ai = AsyncMock(return_value="今天想吃牛肉麵，有人要一起嗎")
        fresh = await worker._generate_context_topic(-5428680940)
        assert fresh == "今天想吃牛肉麵，有人要一起嗎"
        assert worker._normalized_reply(fresh) in worker._recent_proactive_topics

    asyncio.run(main())


def test_context_topic_returns_empty_when_model_keeps_repeating():
    """模型只會復讀時回空字串：這一輪不開口，不塞預設句。"""

    async def main():
        from unittest.mock import AsyncMock

        worker = _worker(303, db=_TopicDB())
        worker._call_ai = AsyncMock(return_value="週末想唱歌")
        assert await worker._generate_context_topic(-5428680940) == "週末想唱歌"
        assert await worker._generate_context_topic(-5428680940) == ""

    asyncio.run(main())


def test_context_topic_drops_and_next_cycle_avoids_same_topic():
    """跨 cycle 反重複：被「repeated topic」drop 的句子要記進記憶，
    下一個 cycle 的 prompt 必須餵回去，逼模型換說法。

    實測根因（小天後 12h：163 次 drop 只有 6 次 sent）：already 每個 cycle
    從零開始，模型反覆生成同一批露骨短句、每次都被正確 drop、下一 cycle 又抽到。
    """

    async def main():
        from unittest.mock import AsyncMock

        old = "早啊\n還賴在床上\n想被幹🫶"
        worker = _worker(404, db=_TopicDB(history=[old]))
        await worker.reload_proactive_memory()
        captured: list[str] = []

        worker._call_ai = AsyncMock(side_effect=lambda system_prompt, prompt, **kw: (
            captured.append(prompt), old)[1]
        )
        assert await worker._generate_context_topic(-5428680940) == ""
        assert len(captured) == 3, "同一句連撞三次應該全部用完"

        # 記憶有記下被 drop 的句子
        assert worker._proactive_recent_seen.get(-5428680940), "drop 的句子要進跨 cycle 記憶"

        # 下一 cycle：第一次呼叫的 prompt 就必須餵回舊句
        worker._call_ai = AsyncMock(return_value="今天下班去吃火鍋")
        fresh = await worker._generate_context_topic(-5428680940)
        assert fresh == "今天下班去吃火鍋"
        first_prompt = worker._call_ai.call_args_list[0].args[1]
        assert old[:4] in first_prompt, "跨 cycle：上輪被 drop 的句子要餵進本輪 prompt"

    asyncio.run(main())


def test_context_topic_prompt_includes_other_accounts_recent_topics():
    """跨帳號話題提示：同群其他水軍最近送出的句子要餵進 prompt，
    避免多號同一時段炒同一主題（實測正午三號齊刷「餓/吃飯」）。

    去重仍靠 remote_texts（撞句回空）；這裡只驗證「模型看得到別號最近講過什麼」。
    """

    async def main():
        from unittest.mock import AsyncMock

        other = "中午餓死了\n誰要約飯🫶"
        worker = _worker(505, db=_TopicDB(history=[other]))
        prompts: list[str] = []
        worker._call_ai = AsyncMock(side_effect=lambda s, p, **kw: (prompts.append(p), "晚上想吃火鍋")[1])
        fresh = await worker._generate_context_topic(-5428680940)
        assert fresh == "晚上想吃火鍋"
        assert prompts, "至少呼叫一次模型"
        assert other.replace("\n", " ")[:20] in prompts[0], "其他帳號最近句子要餵進 prompt"

    asyncio.run(main())


def test_proactive_cooldown_is_shared_within_slot_and_jitters_across_slots():
    """冷卻時間要抖動（反節拍器），但同一窗口內三個帳號必須算出同一個值。

    生產實測：固定 5 分鐘冷卻 → 訊息間隔 325±7 秒的節拍器，一眼看出不是真人。
    """

    async def main():
        worker = _worker(101)
        interval = 300.0
        group = -5565520321
        # 同窗口同值（同一 slot 三個帳號不會各自擲骰搶發）
        same = [worker._proactive_cooldown(group, 12345, interval) for _ in range(5)]
        assert len(set(same)) == 1
        # 落在 0.8~2.4 倍之間
        assert interval * 0.8 <= same[0] <= interval * 2.4
        # 跨窗口不規則：值要分散，而不是全部黏在同一個數字
        values = [worker._proactive_cooldown(group, slot, interval) for slot in range(12345, 12375)]
        assert len(set(values)) > 20
        assert max(values) - min(values) > interval * 0.5
        # 不同群各自獨立
        assert worker._proactive_cooldown(-1001, 12345, interval) != same[0]

    asyncio.run(main())


def test_proactive_loop_releases_slot_when_generation_fails(monkeypatch):
    """claim 成功但生不出話題時，必須歸還窗口。

    不歸還的代價：整個窗口（5 分鐘）全組人一起閉嘴（實測群裡靜 20 分鐘）。
    """

    async def main():
        import time
        from unittest.mock import AsyncMock, Mock

        worker = _worker(101)
        group = -5428680940
        worker.is_running = True
        worker._last_activity = {group: time.time()}
        worker._known_groups = {group}
        worker.config.proactive_enabled = True
        worker.config.proactive_loop_min_seconds = 1.0
        worker.config.proactive_loop_max_seconds = 1.0
        worker.config.proactive_max_per_day = 10
        worker.config.proactive_min_interval_minutes = 1
        worker._is_sleeping = Mock(return_value=False)
        worker._is_busy_hour = Mock(return_value=False)
        worker._should_suppress_proactive = Mock(return_value=False)
        worker._proactive_gate_blocks = Mock(return_value=False)
        worker.db.claim_proactive_slot = AsyncMock(return_value=True)
        worker.db.get_group_messages = AsyncMock(return_value=[])
        released = []
        worker.db.release_proactive_slot = AsyncMock(
            side_effect=lambda *args: (released.append(args), True)[1]
        )
        worker._generate_context_topic = AsyncMock(return_value="")

        ticks = {"n": 0}

        async def controlled_sleep(_delay):
            ticks["n"] += 1
            if ticks["n"] > 2:
                raise asyncio.CancelledError

        monkeypatch.setattr("app.worker.asyncio.sleep", controlled_sleep)
        await worker._proactive_loop()

        assert released, "生不出話題就必須歸還窗口"
        assert released[0][0] == group
        assert released[0][2] == worker.account_id

    asyncio.run(main())


def test_claim_group_text_blocks_cross_account_duplicate():
    """同群同文案 1 小時內只允許第一個帳號發出（DB 層跨帳號攔截）。"""
    import os
    import tempfile

    async def main():
        from app.database import Database

        with tempfile.TemporaryDirectory() as td:
            db = Database(os.path.join(td, "t.db"))
            await db.connect()
            ok1 = await db.claim_group_text(-100, "同一句話", "acct-a")
            ok2 = await db.claim_group_text(-100, "同一句話", "acct-b")
            ok3 = await db.claim_group_text(-100, "不同的一句話", "acct-b")
            await db.close()
            assert ok1 is True
            assert ok2 is False, "跨帳號同句必須被攔"
            assert ok3 is True

    asyncio.run(main())
