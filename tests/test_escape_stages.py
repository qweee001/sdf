"""拒答逃生的降暴露面階段：審核擋的是送進去的字面，不是角色卡。

Chen 上線後 16 次逃生全滅（卡裡帶「肉棒」、原句也照送），四階段的改法就是要
在第二階把上下文與被指那句一起砍短，第三階只剩角色卡。
"""

import asyncio
import re
import time
from unittest.mock import AsyncMock

import pytest

from app import worker as worker_mod
from test_proactive_dedup import _TopicDB
from test_worker_reply_arbitration import _worker

MANAGED = {"name2id"}
GROUP = -5428680940


def _esc_worker():
    w = object.__new__(worker_mod.AccountWorker)
    w.name = "Chen"
    w.persona = {"name": "Chen", "age": 38, "gender": "女", "city": "台北", "industry": "服飾店", "personality": "飢渴騷熟女、喜歡年輕強壯肉棒"}
    w.stats = {}
    w._fallback_models = ()
    w._dead_models = {}
    w._counters = {}
    w._pcount = lambda key: w._counters.__setitem__(key, w._counters.get(key, 0) + 1)
    return w


def test_second_stage_shortens_context_and_target():
    async def main():
        w = _esc_worker()
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
        w = _esc_worker()
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


def test_fourth_stage_drops_the_personality_line():
    """個性裡帶露骨字（Chen 寫「肉棒」）時，最後一階連個性都不給。"""

    async def main():
        w = _esc_worker()
        systems: list[str] = []

        async def fake_call(system_prompt, user_message, model=None, purpose="text"):
            systems.append(system_prompt)
            if "肉棒" in system_prompt:
                return "The prompt could not be submitted."
            return "自己一個人也行啊"

        w._call_ai = AsyncMock(side_effect=fake_call)
        assert await w._refusal_escape(["a：b"], target="肉棒") == "自己一個人也行啊"
        assert "肉棒" not in systems[-1]
        assert w._counters["refusal_escape_stage4"] == 1

    asyncio.run(main())


def test_all_stages_refused_counts_every_try_and_returns_empty():
    async def main():
        w = _esc_worker()
        w._fallback_models = ("backup-a",)
        w._call_ai = AsyncMock(return_value="Request is blocked due to the PUP.")
        assert await w._refusal_escape(["a：b"]) == ""
        assert w.stats["refusal_escape_try"] == 8  # 3 階段 × 2 個模型
        assert "refusal_escape" not in w.stats
        assert "refusal_escape_stage2" not in w._counters

    asyncio.run(main())


def test_refusal_with_trailing_line_is_salvaged():
    async def main():
        w = _esc_worker()
        w._call_ai = AsyncMock(
            return_value="我無法生成露骨內容。\n\n「好啊 你來台北我帶你去」"
        )
        assert await w._refusal_escape(["a：b"]) == "好啊 你來台北我帶你去"
        assert w.stats["refusal_escape"] == 1

    asyncio.run(main())


def test_short_band_cap_is_band_top_not_plus_two(monkeypatch):
    """短帶上限只到 16：+2 會讓 17~18 字整批過關，「8~16」形同虛設。

    10:05 起取消生成後的硬剪，上限改由提示詞自己咬住，所以這裡驗的是送進模型的
    那行字限是 18 字元、且長句原樣保留（不再被剪成殘句）。
    """

    monkeypatch.setattr(worker_mod.random, "random", lambda: 0.1)  # 冷場 0.1＝短碎帶

    async def main():
        worker = _worker(202, db=_TopicDB())
        long_line = "剛擦完乳液看你們在聊鹹蛋腰都軟掉了"
        worker._call_ai = AsyncMock(return_value=long_line)
        topic = await worker._generate_context_topic(GROUP)
        prompt = worker._call_ai.await_args.args[1]
        assert "【長度是上限不是建議】" in prompt
        cap = int(re.search(r"整則最多 (\d+) 字元", prompt).group(1))
        assert cap <= 16, prompt[-200:]
        assert topic == long_line

    asyncio.run(main())


@pytest.mark.parametrize(
    "raw,cut,expect",
    [("看得姊姊身影都濕了", "看得姊姊身", "看得姊姊身影")],
)
def test_cut_never_leaves_half_a_word(raw, cut, expect):
    assert worker_mod.AccountWorker._extend_cut_word(raw, cut) == expect



def test_recent_total_failure_starts_at_stage_two(monkeypatch):
    """十分鐘內才四階全滅過：直奔第二階，別再拿整包上下文撞同一道審核。"""

    async def main():
        w = _esc_worker()
        w._refusal_heat = {}
        seen: list[str] = []
        w._call_ai = AsyncMock(side_effect=lambda _s, body, **_k: seen.append(body) or "The prompt could not be submitted.")

        assert await w._refusal_escape(["a：b", "c：d"], group_id=-1) == ""
        assert -1 in w._refusal_heat

        seen.clear()
        w._call_ai = AsyncMock(side_effect=lambda _s, body, **_k: seen.append(body) or "你倒是來啊")
        assert await w._refusal_escape(["a：b", "c：d"], group_id=-1) == "你倒是來啊"
        assert seen[0].startswith("剛剛在聊"), seen[0]
        assert "群組最近在講" not in seen[0]

    asyncio.run(main())


def test_success_on_stage_one_clears_the_heat():
    async def main():
        w = _esc_worker()
        # 熱度過期（20 分鐘前）：回到第一階，成功就清掉。
        w._refusal_heat = {-1: time.time() - 1200}
        w._call_ai = AsyncMock(return_value="剛脫下高跟鞋腿還痠")
        assert await w._refusal_escape(["a：b"], group_id=-1) == "剛脫下高跟鞋腿還痠"
        assert -1 not in w._refusal_heat
        assert "refusal_escape_hot" not in w._counters

    asyncio.run(main())
