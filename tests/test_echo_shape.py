"""播報腔（_echo_shape_note）：四號輪流複述真人的哏＋補一句裁判評語。

實測 2026-11-04 15:26~16:24 一小時十四則水軍，十一則是「打成蛋餅是怎樣」
「鱷魚眼淚流一流就變老婆了」「多出一百塊連買店裡一件內衣都不夠」這種形狀：
開頭先搬別人剛講的詞，後面補一句評語。自曝只有一則（16:03 伸懶腰）。
單則的複讀擋得住（_local_veto），四號輪著講就擋不住，所以把「這則在講別人」
記下來，四則裡兩則就強制改成用自己的場景開場。

兩條判定：
- 這則開頭直接搬了真人近 12 句裡的字（_mark_echo_if_any，由閘門標記）
- 整句在講別人、自己身上一句都沒講（_is_press_box，直接看文本）
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app import worker as worker_mod
from app.worker import AccountWorker


def _worker(persona_name: str = "Chen") -> AccountWorker:
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
    w.persona = {"name": persona_name, "gender": "女", "age": 38}
    return w


@pytest.fixture(autouse=True)
def _clear_marks():
    worker_mod._ECHO_MARKS.clear()
    yield
    worker_mod._ECHO_MARKS.clear()


def _sent(w: AccountWorker, *bodies: str, gid: int = -1001, ago: float = 0) -> None:
    """把句子塞進群組記憶（ago 秒前），時間戳一起寫才吃得到時間窗。"""
    now = time.time()
    for body in bodies:
        w._recent_texts_by_group.setdefault(gid, []).append(body)
        w._recent_texts_at.setdefault(gid, []).append(now - ago)


def _event(gid: int = -1001) -> SimpleNamespace:
    return SimpleNamespace(chat_id=gid, sender_id=777, text="", is_reply=False)


# ---- 引述標記 ------------------------------------------------------------

def test_mark_only_when_opening_copies_human():
    w = _worker()
    w._mark_echo_if_any(-1001, "打成蛋餅是怎樣，下午不用上班是不是", "你是不是想被打成蛋餅了")
    w._mark_echo_if_any(-1001, "只要在床上體力好把人按著幹到腿軟", "我老婆是鈴鐺")
    assert (-1001, "打成蛋餅是怎樣，下午不用上班是不是") in worker_mod._ECHO_MARKS
    assert (-1001, "只要在床上體力好把人按著幹到腿軟") not in worker_mod._ECHO_MARKS


def test_mark_ignores_empty_humans():
    w = _worker()
    w._mark_echo_if_any(-1001, "打成蛋餅是怎樣", "")
    assert not worker_mod._ECHO_MARKS


# ---- 旁觀判定 ------------------------------------------------------------

def test_press_box_is_line_about_others_without_self():
    assert AccountWorker._is_press_box("打成蛋餅是怎樣，下午不用上班是不是")
    assert AccountWorker._is_press_box("這群哥哥連男人都要搶")
    assert AccountWorker._is_press_box("一個想娶一個想撿現成，人家老公還在場欸🤣")


def test_self_disclosure_is_not_press_box():
    assert not AccountWorker._is_press_box(
        "鱷魚皮太硬哪有我好玩，剛伸懶腰裙子往上縮到大腿根"
    )


def test_line_with_body_cue_is_not_press_box():
    assert not AccountWorker._is_press_box("只要在床上體力好把人按著幹到腿軟")


def test_scene_state_counts_as_self_disclosure():
    """「趁我還在辦公室摸魚」有場景、有「我」，只是沒有身體字——不能判成播報，
    否則場景錨點寫了照樣被退，白燒一次重寫。"""
    assert not AccountWorker._is_press_box(
        "她不想吃雞雞我倒是很想吃，趁我還在辦公室摸魚，哪個哥哥下班要帶來餵飽我的嘴🤣"
    )
    assert not AccountWorker._is_press_box("我剛收攤，鐵門拉下來整個人癱在椅子上")


def test_report_line_stays_press_box():
    """18:00 前後實測：整句只有別人的動作＋一句評語。"""
    assert AccountWorker._is_press_box("尿到鍋裡也太噁了吧，方sir是喝多少")
    assert AccountWorker._is_press_box("萬哥這麼聽話，叫他往後就往後，對我也沒這麼乖過")
    assert AccountWorker._is_press_box("森森是誰啊，突然一堆人誇他厲害")


# ---- 密度判定 ------------------------------------------------------------

def test_two_of_last_four_is_press_box():
    w = _worker()
    _sent(
        w,
        "打成蛋餅是怎樣，下午不用上班是不是",
        "娶人妻這麼刺激，婚禮上你敢不敢牽新娘的手",
        "鈴鐺是要休幾天，群裡哪個弟弟先排隊候補",
        "只要在床上體力好把人按著幹到腿軟",
    )
    # 前三則都在講別人的事，最後一則有「腿軟」算講到自己
    assert w._echo_shape_hits(-1001) == 3
    note = w._echo_shape_note(-1001)
    assert "都在講別人的事" in note
    assert "場邊播報" in note


def test_single_press_box_is_not_enough():
    w = _worker()
    _sent(
        w,
        "只要在床上體力好把人按著幹到腿軟",
        "剛伸懶腰裙子往上縮",
        "冷氣太強腳都冰了",
        "打成蛋餅是怎樣",
    )
    assert w._echo_shape_hits(-1001) == 1
    assert w._echo_shape_note(-1001) == ""


def test_old_lines_fall_out_of_window():
    w = _worker()
    _sent(
        w,
        "打成蛋餅是怎樣",
        "娶人妻這麼刺激",
        "鈴鐺是要休幾天",
        "體力好按著幹到腿軟",
        ago=45 * 60,
    )
    assert w._echo_shape_hits(-1001) == 0


def test_needs_full_lookback_sample():
    w = _worker()
    _sent(w, "打成蛋餅是怎樣", "娶人妻這麼刺激")
    assert w._echo_shape_hits(-1001) == 0


def test_groups_are_isolated():
    w = _worker()
    _sent(w, "打成蛋餅是怎樣", "娶人妻這麼刺激", gid=-2002)
    _sent(w, "打成蛋餅是怎樣", "娶人妻這麼刺激", "鈴鐺要休幾天", "體力好腿軟", gid=-1001)
    assert w._echo_shape_hits(-2002) == 0
    assert w._echo_shape_hits(-1001) == 3


def test_marked_line_counts_even_with_body_cue():
    """有身體字但整句是搬別人原話，仍算一則（兩條判定取聯集）。"""
    w = _worker()
    line = "腿軟是什麼感覺"
    w._mark_echo_if_any(-1001, line, "腿軟是什麼感覺啊")
    _sent(w, line, "冷氣太強腳都冰了", "床好涼", "整理蕾絲睡衣")
    assert w._echo_shape_hits(-1001) == 1


def test_needs_retry_only_when_press_box_pressure():
    w = _worker()
    _sent(w, "打成蛋餅是怎樣", "娶人妻這麼刺激", "鈴鐺要休幾天", "體力好腿軟")
    assert w._needs_echo_retry(-1001, "這邏輯我服")
    assert not w._needs_echo_retry(-1001, "剛伸懶腰裙子往上縮")


def test_no_retry_without_pressure():
    w = _worker()
    _sent(w, "剛伸懶腰腰好痠", "床好涼腿夾著", "腳都冰了", "整理睡衣腰好酸")
    assert not w._needs_echo_retry(-1001, "這邏輯我服")


# ---- 主動發言路徑（這一群幾乎全靠主動，回覆只有一則） -------------------

def _stub_pick(w: AccountWorker, result: str, calls: list[Any]) -> None:
    async def fake(*_a, **_k):
        calls.append(1)
        return result

    w._pick_passing_candidate = fake  # type: ignore[assignment]


def test_proactive_rewrite_replaces_press_line():
    import asyncio

    w = _worker()
    _sent(w, "打成蛋餅是怎樣", "娶人妻這麼刺激", "鈴鐺是要休幾天", "體力好腿軟")
    calls: list[Any] = []
    _stub_pick(w, "剛收攤腳底板發燙，脫了鞋踩地板散熱", calls)
    out = asyncio.run(
        w._proactive_echo_rewrite(-1001, "這邏輯我服", "", [], "")
    )
    assert out == "剛收攤腳底板發燙，脫了鞋踩地板散熱"
    assert calls and w.stats["proactive_echo_rewrite"] == 1


def test_proactive_rewrite_skips_without_pressure():
    import asyncio

    w = _worker()
    _sent(w, "剛伸懶腰腰好痠", "床好涼腿夾著", "腳都冰了", "整理睡衣腰好酸")
    calls: list[Any] = []
    _stub_pick(w, "不該被用到", calls)
    assert asyncio.run(w._proactive_echo_rewrite(-1001, "這邏輯我服", "", [], "")) == ""
    assert not calls


def test_proactive_rewrite_falls_back_to_original_when_rewrite_fails():
    import asyncio

    w = _worker()
    _sent(w, "打成蛋餅是怎樣", "娶人妻這麼刺激", "鈴鐺是要休幾天", "體力好腿軟")
    _stub_pick(w, "", [])
    assert asyncio.run(w._proactive_echo_rewrite(-1001, "這邏輯我服", "", [], "")) == ""


# ---- 併進導演指令 --------------------------------------------------------

def test_directors_note_forces_scene_when_press_box():
    w = _worker("Chen")
    _sent(
        w,
        "打成蛋餅是怎樣，下午不用上班是不是",
        "娶人妻這麼刺激，婚禮上你敢不敢牽新娘的手",
        "鈴鐺是要休幾天，群裡哪個弟弟先排隊候補",
        "只要在床上體力好把人按著幹到腿軟",
    )
    note = w._build_directors_note(
        _event(), [], own_texts=[], last_human="你是不是想被打成蛋餅了"
    )
    assert "場邊播報" in note
    # 一定要落在場景心境（一定有錨點場景字串），不是純嘴賤那型
    assert re.search(r"【心境：隨性放鬆/生活瑣事】：「", note)


def test_directors_note_clean_group_has_no_press_box_hint():
    w = _worker("Chen")
    _sent(w, "剛拉下鐵門腳底板超痠", "練完瑜珈筋都軟了", "床好涼腿夾著", "整理蕾絲睡衣腰好酸")
    note = w._build_directors_note(_event(), [], own_texts=[], last_human="")
    assert "場邊播報" not in note


# ---- 自曝窗 3→6 ---------------------------------------------------------

def test_one_self_disclosure_no_longer_covers_five_press_boxes():
    """一則自曝後面又連五則播報 → 照樣要講自己。

    舊版只要六則內有任何一則自曝就整段免檢查（一則擋六則，
    實測就是這樣養出 RICH 連四則純評語）。
    """
    w = _worker()
    _sent(
        w,
        "剛伸懶腰裙子往上縮到大腿根",
        "這群哥哥連男人都要搶",
        "一個想娶一個想撿現成",
        "打成蛋餅是怎樣",
        "鈴鐺是要休幾天",
        "多出一百塊連買店內衣都不夠",
    )
    assert "沒人講到自己" in w._sentence_type_note([], -1001, [])


def test_six_lines_without_self_disclosure_fires():
    w = _worker()
    _sent(
        w,
        "剛伸懶腰裙子往上縮到大腿根",
        "這群哥哥連男人都要搶",
        "一個想娶一個想撿現成",
        "打成蛋餅是怎樣",
        "鈴鐺是要休幾天",
        "多出一百塊連買店內衣都不夠",
        "最後一則評語",
    )
    assert "沒人講到自己" in w._sentence_type_note([], -1001, [])


# ---- 生活詞時鐘窗 -------------------------------------------------------

def test_stale_sentence_after_an_hour_is_flagged():
    """14:08 Chen 微冰手搖 → 15:39 小天後 請我喝杯手搖，91 分鐘，df 只有 1。"""
    w = _worker()
    _sent(w, "先請姊姊喝杯微冰手搖", ago=91 * 60)
    _sent(w, "哪像有些人只會出一張嘴", ago=70 * 60)
    _sent(w, "這群哥哥連男人都要搶", ago=20)
    _sent(w, "打成蛋餅是怎樣", ago=5)
    note = w._overused_terms_note(-1001, [])
    assert "手搖" in note
    assert "分鐘前" in note


def test_fresh_lines_are_not_stale():
    w = _worker()
    _sent(w, "剛拉下鐵門腳底板超痠", "練完瑜珈筋都軟了", "床好涼腿夾著")
    note = w._overused_terms_note(-1001, [])
    assert "分鐘前" not in note
