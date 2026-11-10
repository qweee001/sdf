"""尾巴通道：真人短信堆裡偶爾會插一句長的，水軍不能永遠是平頭。

2026-11-10 15:07–17:07 實測：真人中位 8 字、p90 14、6 則 ≥23 字（最長 102），
水軍 73 則中位 13、最大 22、離散度 3.9（真人 8.3），兩小時零長句。原因是四個
模式的天花板全部乘在真人中位數上（1.4／1.7／2.0／2.2），中位 8 就被算成
11／13／16／17，尾巴整段被剪掉。

修法：另開尾巴通道（_len_tail）。抽到的那則改走能容長句的模式，上限吃人類 p90，
地板同時跟群友一起往下放，四五字反應才出得來。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1007


def _worker(name: str = "Chen") -> AccountWorker:
    config = SimpleNamespace(
        ai_model="m",
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
        managed_ids=set(),
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[GID],
    )
    w.persona = {"name": name, "gender": "女", "age": 28}
    return w


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        chat_id=GID, sender_id=1, message_id=1, text="", raw_text="",
        media=None, is_reply=False, sender=None, reply_to=None, message=None,
    )


def _short_crowd(n: int = 20) -> list[dict]:
    """真人集體三五字的現場，p90 只有 14 字。"""
    lens = [4, 5, 5, 6, 6, 6, 7, 7, 7, 8, 8, 8, 9, 9, 10, 10, 11, 12, 13, 14]
    return [{"role": "user", "content": "字" * lens[i % n]} for i in range(n)]


def _note_with(*rolls: float):
    """依序餵 random.random()，用完回 1.0（＝不抽中任何低機率分支）。

    導演指令一條要吃掉三次骰子（模式、窄帶、尾巴），單一個常數骰不到尾巴，
    所以這裡用序列：0.5 → dirty、0.9 → 不窄帶、0.1 → 抽中尾巴。
    """
    w = _worker()
    seq = list(rolls)
    old = random.random
    random.random = lambda: seq.pop(0) if seq else 1.0
    try:
        note = w._build_directors_note(
            _event(), history=_short_crowd(), own_texts=[], last_human=""
        )
    finally:
        random.random = old
    m = re.search(r"以 (\d+)~(\d+) 字內", note)
    assert m, note
    return w, note, int(m.group(1)), int(m.group(2))


TAIL_ROLLS = (0.5, 0.9, 0.1)     # dirty → 不窄帶 → 尾巴
PLAIN_ROLLS = (0.5, 0.9, 0.9)    # dirty → 不窄帶 → 沒有尾巴


def test_tail_roll_lifts_ceiling_past_short_crowd_median():
    """抽到尾通道就必須突破中位倍率，兩小時零長句的平頭才會散開。"""
    w = _worker()
    w._struct_mode = "dirty"
    w._human_ref = 8          # 真人中位 8 字：正常算出來是 16
    w._len_tail = True
    w._len_tail_ref = 14
    assert 16 == w._struct_char_cap(90) - 0 or w._struct_char_cap(90) > 22
    # 22 字是這兩小時的實際天花板；尾巴至少要能過它
    assert w._struct_char_cap(90) > 22
    w._struct_mode = "scene"
    assert w._struct_char_cap(90) > 22


def test_no_tail_keeps_tight_ceiling():
    w, note, _lo, hi = _note_with(*PLAIN_ROLLS)
    assert w._len_tail is False
    assert hi <= 20
    assert "這則刻意寫長" not in note


def test_tail_never_fires_on_short_tight_band():
    """窄帶短碎本來就三五字，尾巴留給其它模式。"""
    w, _note, _lo, _hi = _note_with(0.0, 0.9, 0.1)   # 短碎 → 沒抽到碎字 → 窄帶
    assert w._short_tight is True
    assert w._len_tail is False


def test_band_floor_follows_terse_crowd():
    """真人中位 8 字時，8 字地板要跟著放低，四五字反應才出得來（73 則只有 6 則 <8）。"""
    _w, _note, lo, _hi = _note_with(0.0, 0.5, 0.9)
    assert 4 <= lo <= 7


def test_tail_probability_is_counted_and_rare():
    """尾巴是「偶爾」，不是常態：種子連跑 300 次，比例落在 8~30%。"""
    w = _worker()
    random.seed(7)
    for _ in range(300):
        w._build_directors_note(_event(), history=_short_crowd(), own_texts=[], last_human="")
    hits = w.stats.get("band_len_tail", 0)
    assert 20 <= hits <= 90, hits


def test_human_len_tail_reads_p90_not_median():
    w = _worker()
    crowd = _short_crowd()
    med = w._human_len_ref(crowd)
    tail = w._human_len_tail(crowd)
    assert med == 8
    assert tail is not None and tail > med


def test_name_opener_axis_counts_named_starts():
    w = _worker()
    w._speaker_names_by_group[GID] = ["戰豆", "鈴鐺", "華發"]
    w._cross_recent_texts = lambda *_a, **_k: [
        "戰豆懂我", "華發你也只剩張嘴", "下午摸魚也救不了",
    ]
    assert w._name_open_hits(GID) == 2
    note = w._sentence_type_note([], GID, [])
    assert "喊人名" in note


def test_dirty_judge_axis_fires_when_explicit_lines_judge_him():
    """露骨但全在評男人尺寸（實測四則強露骨都是這種），要逼回自己的身體。"""
    w = _worker()
    w._speaker_names_by_group[GID] = ["戰豆"]
    w._cross_recent_texts = lambda *_a, **_k: [
        "你說會喜歡我，下午摸魚也救不了小雞雞可憐",
        "方sir那你的雞雞能撐多久",
        "下午摸魚也救不了",
    ]
    assert w._dirty_judge_hits([], GID, []) == 2
    note = w._sentence_type_note([], GID, [])
    assert "都在評他的尺寸" in note


def test_dirty_judge_axis_quiet_when_self_body_present():
    w = _worker()
    w._speaker_names_by_group[GID] = ["戰豆"]
    w._cross_recent_texts = lambda *_a, **_k: [
        "斷了多可惜，好用的肉棒要留給姐姐含啊",
        "底下已經濕一片了，你快點",
        "下午摸魚也救不了",
    ]
    assert w._dirty_judge_hits([], GID, []) == 0


def test_proactive_prompt_carries_name_and_dirty_axes():
    """主動路徑原本只吃字數帶，點名軸／尺度軸全缺（17:53–18:00 四則全喊名開場）。"""
    w = _worker()
    w.persona.update({
        "hobbies": ["追劇"], "looking_for": "約會", "city": "台北",
        "district": "大安", "industry": "服飾", "university": "台大",
        "personality": "悶騷", "meetups_done": 3,
    })
    gid = -1002229799107
    w.selected_groups = {gid}
    w._speaker_names_by_group[gid] = ["戰豆", "鈴鐺", "規範"]
    now = time.time()
    w._recent_texts_by_group[gid] = [
        "戰豆這婚明顯結不成了",
        "規範別光看戲，被嗆到耳根發燙",
    ]
    w._recent_texts_at[gid] = [now - 60, now - 60]
    prompts: list[str] = []

    class _DB:
        async def get_group_messages(self, group_id, limit=100):
            return [
                {"sender_name": "戰豆", "sender_id": 5, "role": "user",
                 "content": "我雞雞1公分", "timestamp": time.time()},
            ]

        async def get_group_shared_notes(self, group_id, account_id, limit=10):
            return []

        async def recent_bot_texts_by_group(self, group_id):
            return list(w._recent_texts_by_group[gid])

    w.db = _DB()

    async def _ai(*args, **k):
        prompts.append(args[-1] if len(args) > 1 else args[0])
        return "標準啦，站起來就頂到我了"

    w._call_ai = _ai
    asyncio.run(w._generate_context_topic(gid))
    assert prompts, "主動路徑沒有發生成"
    assert any("不要喊名字" in p for p in prompts)
