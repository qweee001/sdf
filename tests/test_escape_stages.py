"""拒答逃生的降暴露面階段：審核擋的是送進去的字面，不是角色卡。

Chen 上線後 16 次逃生全滅（卡裡帶「肉棒」、原句也照送），四階段的改法就是要
在第二階把上下文與被指那句一起砍短，第三階只剩角色卡。
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app import worker as worker_mod

MANAGED = {"name2id"}


def _worker():
    w = object.__new__(worker_mod.AccountWorker)
    w.name = "Chen"
    w.persona = {"name": "Chen", "age": 38, "gender": "女", "city": "台北", "industry": "服飾店"}
    w.stats = {}
    w._fallback_models = ()
    w._counters = {}
    w._pcount = lambda key: w._counters.__setitem__(key, w._counters.get(key, 0) + 1)
    return w


def test_second_stage_shortens_context_and_target():
    async def main():
        w = _worker()
        seen: list[str] = []

        async def fake_call(system_prompt, user_message, model=None, purpose="text"):
            seen.append(user_message)
            if "群組最近在講" in user_message:
                return "The prompt could not be submitted."
            return "剛脫下高跟鞋腿還痠"

        w._call_ai = AsyncMock(side_effect=fake_call)
        lines = ["方sir：雞雞別打他", "小小：插進我小穴試試", "Chen：姊姊的肉棒要嗎"]
        text = await w._refusal_escape(lines, target="插進我小穴試試會不會送醫")
        assert text == "剛脫下高跟鞋腿還痠"
        assert len(seen) == 2
        assert "群組最近在講" in seen[0]
        assert "這一句：插進我小穴試試會不會送醫" in seen[0]
        assert "剛剛在聊" in seen[1]
        assert "這一句（砍短）：插進我小穴試試會不會" in seen[1]
        assert w._counters["refusal_escape_stage2"] == 1
        assert w.stats["refusal_escape"] == 1

    asyncio.run(main())


def test_third_stage_drops_context_entirely():
    async def main():
        w = _worker()
        seen: list[str] = []

        async def fake_call(system_prompt, user_message, model=None, purpose="text"):
            seen.append(user_message)
            if "沒什麼人講話" in user_message:
                return "自己一個人也行啊"
            return "The prompt could not be submitted."

        w._call_ai = AsyncMock(side_effect=fake_call)
        text = await w._refusal_escape(["a：b"], target="肉棒")
        assert text == "自己一個人也行啊"
        assert w._counters["refusal_escape_stage3"] == 1
        assert "群組現在沒什麼人講話" in seen[-1]

    asyncio.run(main())


def test_all_stages_refused_counts_every_try_and_returns_empty():
    async def main():
        w = _worker()
        w._fallback_models = ("backup-a",)
        w._call_ai = AsyncMock(return_value="Request is blocked due to the PUP.")
        assert await w._refusal_escape(["a：b"]) == ""
        assert w.stats["refusal_escape_try"] == 6  # 3 階段 × 2 個模型
        assert "refusal_escape" not in w.stats
        assert "refusal_escape_stage2" not in w._counters

    asyncio.run(main())


def test_refusal_with_trailing_line_is_salvaged():
    async def main():
        w = _worker()
        w._call_ai = AsyncMock(
            return_value="我無法生成露骨內容。\n\n「好啊 你來台北我帶你去」"
        )
        assert await w._refusal_escape(["a：b"]) == "好啊 你來台北我帶你去"
        assert w.stats["refusal_escape"] == 1

    asyncio.run(main())


@pytest.mark.parametrize(
    "raw,cut,expect",
    [
        ("看得姊姊身影都濕了", "看得姊姊身", "看得姊姊身影"),
        ("跟姊姊撒嬌一下", "跟姊姊撒", "跟姊姊撒嬌"),
        ("剛擦乳液，看你們", "剛擦乳液", "剛擦乳液"),
        ("好累喔", "好累", "好累喔"),
    ],
)
def test_cut_never_leaves_half_a_word(raw, cut, expect):
    assert worker_mod.AccountWorker._extend_cut_word(raw, cut) == expect
