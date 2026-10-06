"""錯別字決策層：對照表（免費）＋ Jev 專門問「這段有沒有用字錯誤」。

跟 ③ 分層：③ 管語意（離題／矛盾／語氣／時段），這層只盯字形與詞形。
實測動機：qwen 產生過「中坢」（地名別字）與常見同音字錯用，整條管線原本沒人管用字。
"""

import asyncio

from test_worker_reply_arbitration import _worker

GROUP = -5565520321


def _decision_worker() -> object:
    """決策層要開（typo 層才有作用）：測試用的 _worker 不吃 key，這裡補上。"""
    worker = _worker(101)
    worker.config.decision_api_key = "k"
    worker.config.decision_typo_threshold = 0.5
    return worker


def test_common_typo_table_hits():
    async def main():
        worker = _worker(101)
        assert "應該" in worker._common_typo_hint("我因該晚點到")
        assert "時候" in worker._common_typo_hint("你時侯到了嗎")
        assert "處理" in worker._common_typo_hint("這件事我處裡一下")
        assert "迫不及待" in worker._common_typo_hint("我迫不急待想見你")

    asyncio.run(main())


def test_correct_text_is_not_flagged():
    async def main():
        worker = _worker(101)
        for text in (
            "我應該晚點到",
            "你時候到了嗎",
            "這件事我處理一下",
            "我迫不及待想見你",
            "先練練舌頭功",
            "那你來試試看🫦",
            "等等要吃什麼",
        ):
            assert worker._common_typo_hint(text) == "", text

    asyncio.run(main())


def test_typo_review_parses_decision_layer(monkeypatch):
    async def main():
        import app.worker as worker_mod

        worker = _decision_worker()

        async def fake(state, questions, **kw):
            assert "用字錯誤" in questions["has_typo"]["instructions"]
            return {"has_typo": {"noul": 0.92}, "kind": {"choice": "wrong_char"}}

        monkeypatch.setattr(worker_mod, "system_one", fake)
        review = await worker._typo_review("我因該晚點到", "上下文")
        assert review == {"prob": 0.92, "kind": "wrong_char"}
        assert worker._typo_flag(review) is True
        assert worker._typo_flag({"prob": 0.2, "kind": "wrong_char"}) is False
        # kind=none 時即使機率高也不算（決策層自己說沒問題）
        assert worker._typo_flag({"prob": 0.9, "kind": "none"}) is False

    asyncio.run(main())


def test_typo_review_disabled_without_key():
    async def main():
        worker = _worker("")
        assert await worker._typo_review("我因該晚點到", "") is None
        assert worker._typo_flag(None) is False

    asyncio.run(main())


def test_typo_gate_rewrites_exact_correction(monkeypatch):
    """對照表命中 → 帶正確寫法重寫一次 → 修好就放行。"""

    async def main():
        from unittest.mock import AsyncMock

        import app.worker as worker_mod

        worker = _decision_worker()
        worker._call_ai = AsyncMock(return_value="我應該晚點到")

        async def fake(state, questions, **kw):
            return {"has_typo": {"noul": 0.05}, "kind": {"choice": "none"}}

        monkeypatch.setattr(worker_mod, "system_one", fake)
        event = AsyncMock(
            sender_id=999,
            chat_id=GROUP,
            id=95,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="你幾點到",
            media=None,
        )
        event.sender = None
        fixed = await worker._typo_gate(event, "我因該晚點到")
        assert fixed == "我應該晚點到"
        assert worker.stats.get("typo_rewrite") == 1
        instruction = worker._call_ai.await_args.args[1]
        assert "用字錯誤" in instruction and "應該" in instruction

    asyncio.run(main())


def test_typo_gate_uses_decision_layer_when_table_misses(monkeypatch):
    """對照表抓不到的同音字，交給決策層；重寫後仍被判定有錯就不發。"""

    async def main():
        from unittest.mock import AsyncMock

        import app.worker as worker_mod

        worker = _decision_worker()
        worker._call_ai = AsyncMock(return_value="等等見面在說")
        calls = {"n": 0}

        async def fake(state, questions, **kw):
            calls["n"] += 1
            # 第一次判定有錯字，重寫後仍判定有錯 → 暫緩
            return {"has_typo": {"noul": 0.88}, "kind": {"choice": "wrong_word"}}

        monkeypatch.setattr(worker_mod, "system_one", fake)
        event = AsyncMock(
            sender_id=999,
            chat_id=GROUP,
            id=96,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="等等見面再說",
            media=None,
        )
        event.sender = None
        assert await worker._typo_gate(event, "等等見面在說") == ""
        assert worker.stats.get("typo_rewrite") == 1
        assert worker.stats.get("typo_held") == 1

    asyncio.run(main())


def test_typo_gate_passes_clean_text_without_extra_call(monkeypatch):
    """乾淨的文字不該重寫（決策層說沒有問題就直接過）。"""

    async def main():
        import app.worker as worker_mod

        worker = _decision_worker()
        called = {"n": 0}

        async def fake(state, questions, **kw):
            called["n"] += 1
            return {"has_typo": {"noul": 0.03}, "kind": {"choice": "none"}}

        monkeypatch.setattr(worker_mod, "system_one", fake)
        out = await worker._typo_gate(None, "等等見面再說")
        assert out == "等等見面再說"
        assert called["n"] == 1
        assert not worker.stats.get("typo_rewrite")

    asyncio.run(main())
