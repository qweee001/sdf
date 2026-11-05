"""起手詞撞車／複讀真人／連三問的動態自檢（2026-11-03 18:xx 桃花源實測破口）。

- 小天後 18:04「笑死，7天後發薪水…」→ 18:15「笑死，萬哥連小風住哪都問不到」（11 分鐘同一起手）
- 小小 18:09 真人剛說「小萬正在幹鱷魚」→ 18:10「小萬正在幹鱷魚，這詞也太硬了吧」（整句照抄）
- 11 則水軍有 6 則用問句收尾（55%），指令寫了「絕對禁止」照樣犯

修法都是動態：起手詞由本人 DB 發言現算，複讀由字串比對，不寫死詞表。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker


def _worker(name: str = "小天後") -> AccountWorker:
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
        managed_ids=set(),
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[-1001],
    )
    w.persona = {"name": name, "gender": "女", "age": 30}
    return w


def test_opening_token_skips_quotes_and_punct():
    w = _worker()
    assert w._opening_token("「笑死，7天後才發薪水」") == "笑死"
    assert w._opening_token("  我剛換上蕾絲內衣") == "我剛"
    assert w._opening_token("好") == ""


def test_opening_repeat_note_flags_own_cliche():
    w = _worker()
    own = ["笑死，萬哥連小風住哪都問不到", "笑死，7天後才發薪水", "彭于晏會自己講像彭于晏嗎"]
    note = w._opening_repeat_note(own)
    assert "笑死" in note and "不准" in note


def test_opening_repeat_note_quiet_when_all_different():
    w = _worker()
    own = ["剛換上蕾絲內衣手感超滑", "要我買單可以啊先讓我摸", "鱷魚吃不飽啦"]
    assert w._opening_repeat_note(own) == ""


def test_verbatim_run_catches_copy_of_human_line():
    w = _worker()
    human = "小萬正在幹鱷魚"
    cand = "小萬正在幹鱷魚，這詞也太硬了吧"
    assert w._verbatim_run(cand, human) == "小萬正在幹鱷魚"


def test_verbatim_run_short_text_is_safe():
    w = _worker()
    assert w._verbatim_run("哈囉", "哈囉大家") == ""


def test_local_veto_rejects_verbatim_echo():
    w = _worker()
    veto = w._local_veto("小萬正在幹鱷魚，這詞也太硬了吧", [], "小萬正在幹鱷魚")
    assert "照抄" in veto


def test_local_veto_rejects_third_question_ending():
    w = _worker()
    own = ["萬哥連住哪都問不到，先問到地址再說吧", "今晚是有體力戰幾回合嗎"]
    veto = w._local_veto("先問到地址再說吧", own, "")
    assert "問句" in veto


def test_local_veto_rejects_second_question_ending():
    w = _worker()
    own = ["晚點還要用嘴巴幹嘛呢"]
    veto = w._local_veto("送假的幹嘛，是對自己真的沒信心嗎🤣", own, "")
    assert "問句" in veto


def test_question_tail_sees_through_trailing_emoji():
    w = _worker()
    assert w._bare_tail("送假的幹嘛，是對自己真的沒信心嗎🤣") == "送假的幹嘛，是對自己真的沒信心嗎"
    assert w._bare_tail("「這樣好嗎？」") == "「這樣好嗎"


def test_local_veto_allows_assertion_after_questions():
    w = _worker()
    own = ["萬哥連住哪都問不到，先問到地址再說吧", "今晚是有體力戰幾回合嗎"]
    assert "問句" not in (w._local_veto("萬哥連住哪都問不到，先問到地址再說", own, "") or "")


def test_local_veto_rejects_repeated_own_opening():
    w = _worker()
    own = ["笑死，萬哥連小風住哪都問不到", "笑死，7天後才發薪水"]
    veto = w._local_veto("笑死，這也能吹", own, "")
    assert "起手詞" in veto


def test_local_veto_passes_clean_line():
    w = _worker()
    own = ["笑死，萬哥連小風住哪都問不到", "笑死，7天後才發薪水"]
    assert w._local_veto("彭于晏會自己講像彭于晏嗎", own, "小萬正在幹鱷魚") == ""


def test_directors_note_carries_opening_and_echo_notes():
    w = _worker()
    own = ["笑死，萬哥連小風住哪都問不到", "笑死，7天後才發薪水"]
    note = w._build_directors_note(
        SimpleNamespace(sender_id=999, sender=None), [], own, "小萬正在幹鱷魚"
    )
    assert "笑死" in note
    assert "小萬正在幹鱷魚" in note


def test_anchor_dedup_uses_db_own_texts_not_window():
    """對話窗被別人沖掉時，仍要靠 DB 的本人發言擋掉同一個場景。"""
    w = _worker("Chen")
    anchors = [
        ("店裡", "「服飾店剛拉下鐵門，換掉包臀裙只穿睡袍」"),
        ("瑜珈", "「練完瑜珈全身筋骨都拉開了」"),
    ]
    own = ["害我店裡摸魚滑手機看你們排隊"]
    for _ in range(20):
        assert "服飾店剛拉下鐵門" not in w._pick_life_anchor(anchors, "Chen", [], own)


def test_cross_opening_note_flags_shared_group_cliche():
    w = _worker()
    w._recent_texts_by_group[-1001] = [
        "剛洗完澡頭髮還在滴水",
        "剛洗完澡擦完身體乳",
        "夜市滷味想到你們",
        "紅燒肉配白飯超下飯",
        "怕就別約我又不吃人",
        "保險套留著自己用啦",
    ]
    assert "剛洗" in w._cross_opening_note(-1001)


def test_opening_cliche_survives_an_hour_of_gap():
    """19:53 與 20:58 兩次「剛洗完澡」隔了 65 分鐘，45 分鐘窗完全抓不到。"""
    w = _worker()
    now = time.time()
    texts = ["剛洗完澡頭髮還在滴水"] + ["紅燒肉配白飯超下飯"] * 3 + [
        "剛洗完澡躺著聞沐浴乳香"
    ] + ["怕就別約我又不吃人"] * 2
    ages = [65 * 60, 40 * 60, 30 * 60, 20 * 60, 10 * 60, 5 * 60, 60]
    w._recent_texts_by_group[-1001] = texts
    w._recent_texts_at[-1001] = [now - a for a in ages]
    assert "剛洗" in w._cross_opening_note(-1001)


def test_cross_opening_note_flags_same_head_char_across_accounts():
    """「剛站櫃」「剛開店」「剛擦完身體乳」前兩字互不相同，仍是同一格起手。

    10:00~10:23 三號輪流用「剛＋當下動作」開場，字面比對一句都攔不到。
    """
    w = _worker()
    w._recent_texts_by_group[-1001] = [
        "剛站櫃就想摸魚喝冰美式",
        "剛開店冷氣都還沒涼",
        "剛擦完身體乳滑溜溜",
        "萬英尺你也太壞了吧",
        "好啦別吵了跟自卑的人計較什麼",
        "保險套留著自己用啦",
    ]
    assert "剛" in w._cross_opening_note(-1001)


def test_cross_opening_note_head_char_needs_two():
    """同一個字兩次就點名。

    這一群一小時才七則水軍，等第三次才攔太慢（11:46 小小、11:56 Chen 都「笑死」，
    十分鐘內群友就看到同一格）。樣本不足 6 則仍不判，避免把「我／你」一律封掉。
    """
    w = _worker()
    w._recent_texts_by_group[-1001] = [
        "剛站櫃就想摸魚喝冰美式",
        "剛開店冷氣都還沒涼",
        "夜市滷味想到你們",
        "紅燒肉配白飯超下飯",
        "怕就別約我又不吃人",
        "保險套留著自己用啦",
    ]
    assert "剛" in w._cross_opening_note(-1001)


def test_cross_opening_note_does_not_repeat_head_already_flagged():
    w = _worker()
    w._recent_texts_by_group[-1001] = [
        "剛洗完澡頭髮還在滴水",
        "剛洗完澡擦完身體乳",
        "剛站櫃就想摸魚喝冰美式",
        "夜市滷味想到你們",
        "怕就別約我又不吃人",
        "保險套留著自己用啦",
    ]
    note = w._cross_opening_note(-1001)
    assert "剛洗" in note
    # 2 字詞已點名過，不該再重複列一次同樣的起頭字
    assert "；「剛」" not in note


def test_cross_opening_note_quiet_without_shared_start():
    w = _worker()
    w._recent_texts_by_group[-1001] = [
        "夜市滷味想到你們",
        "紅燒肉配白飯超下飯",
        "怕就別約我又不吃人",
        "保險套留著自己用啦",
    ]
    assert w._cross_opening_note(-1001) == ""
