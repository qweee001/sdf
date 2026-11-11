"""11-11 中午破口：喊人名黏成一句、暱稱被寫成同形字、同一個人被連著點名。

實測（12:03–12:54 桃花源，200 則裡水軍 39 則，長度已散開、無真空）：

- 12:07:49 RICH「範挺懂憐香惜玉嘛」／12:08:30 小小「規範幫妹妹出頭超帥」：群友
  本尊叫「規范」，模型自動正規化成「規範」「範」，兩個都不是他的名字。
- 12:37:22「華發害人家喝奶茶差點噴出來」、12:39:07「請教超會哄欸」：名字直接黏
  下一句，讀起來像名詞卡在句子裡（「請教」還跟動詞同形）。
- 12:50:21／12:51:47／12:54:24 三則都在講森森，而森森當時正在跟鈴鐺對話，四號
  變成場邊轉播。提示層的「這人被輪爛了」攔不住，只能字面擋。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001


def _worker(name: str = "Chen") -> AccountWorker:
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
        selected_groups=[GID],
    )
    w.persona = {"name": name, "gender": "女", "age": 28}
    for n in ("規范", "華發", "請教", "萬英尺", "森森", "風口浪尖", "深情森森"):
        w._remember_speaker_name(GID, n)
    return w


def test_name_glued_to_sentence_gets_a_comma():
    """12:37:22 實句：名字後面直接接下一句，補逗號才讀得出來在喊人。"""
    w = _worker()
    assert (
        w._fix_name_address("華發害人家喝奶茶差點噴出來", GID)
        == "華發，害人家喝奶茶差點噴出來"
    )


def test_name_that_is_also_a_verb_still_breaks():
    """「請教」跟動詞同形，12:39:07「請教超會哄欸」沒有逗號根本判不出是名字。"""
    w = _worker()
    assert w._fix_name_address("請教超會哄欸", GID) == "請教，超會哄欸"


def test_verb_sharing_name_tail_is_not_hijacked():
    """名字最後一個字剛好是常用動詞（華發→發），「發薪水了」不該被改寫。"""
    w = _worker()
    assert w._fix_name_address("發薪水了", GID) == "發薪水了"


def test_variant_name_is_repaired_then_broken():
    """規范→規範是整組同形字，模型照字面正規化，要換回本尊。"""
    w = _worker()
    assert w._fix_name_address("規範幫妹妹出頭超帥", GID) == "規范，幫妹妹出頭超帥"


def test_abbreviated_handle_is_broken_too():
    """15:01 實句「地暖我睏到小穴都癢」：群友叫他地暖，全名地暖鋪著，只喊前段也要斷。"""
    w = _worker()
    w._remember_speaker_name(GID, "地暖鋪著")
    assert w._fix_name_address("地暖我睏到小穴都癢", GID) == "地暖，我睏到小穴都癢"


def test_abbreviation_needs_two_chars():
    """只撞一個字還是別人的名字時不動（免得把詞當人喊）。"""
    w = _worker()
    w._remember_speaker_name(GID, "地暖鋪著")
    assert w._fix_name_address("地瓜好吃的嗎", GID) == "地瓜好吃的嗎"


def test_single_char_variant_name_is_repaired():
    w = _worker()
    assert w._fix_name_address("範挺懂憐香惜玉嘛", GID) == "規范，挺懂憐香惜玉嘛"


def test_name_as_subject_keeps_flowing():
    """「森森在宣示主權」名字是主語，硬加逗號反而斷。"""
    w = _worker()
    src = "森森在宣示主權嗎，那誰來抱我"
    assert w._fix_name_address(src, GID) == src


def test_name_stuck_mid_sentence_gets_a_comma():
    """12:18:43「待在室內萬英尺敢抱著我流汗嗎」：名字卡在句中，前面要斷。"""
    w = _worker()
    assert (
        w._fix_name_address("待在室內萬英尺敢抱著我流汗嗎", GID)
        == "待在室內，萬英尺敢抱著我流汗嗎"
    )


def test_name_as_object_is_left_alone():
    """「聽森森講」名字是受詞，補逗號會斷成「聽，森森，講」。"""
    w = _worker()
    src = "趴桌摸魚聽森森講都夾緊腿了"
    assert w._fix_name_address(src, GID) == src


def test_already_punctuated_name_is_untouched():
    w = _worker()
    src = "內褲早就濕透了，森森等等私訊幫我換掉。"
    assert w._fix_name_address(src, GID) == src


def test_plain_sentence_without_name_is_untouched():
    w = _worker()
    src = "嘴真挑"
    assert w._fix_name_address(src, GID) == src


def test_name_flood_blocks_third_mention():
    """同一人被水軍半小時點三次，第四則再點就要換人。"""
    w = _worker()
    now = time.time()
    others = [
        (300, "趴桌摸魚聽森森講都夾緊腿了"),
        (150, "內褲早就濕透了，森森等等私訊幫我換掉。"),
        (60, "森森在宣示主權嗎，那誰來抱我"),
        (50, "完全插不上話"),
        (40, "我不想上班了"),
        (30, "你們不要在群裡吵起來了"),
        (20, "湯汁紅紅亮亮的"),
        (10, "吃來吃去就吃一點那個味道"),
    ]
    w._recent_texts_by_group[GID] = [t for _a, t in others]
    w._recent_texts_at[GID] = [now - a for a, _t in others]
    assert "森森" in w._named_target_flood("森森準備舔鈴鐺了", GID)
    assert w._named_target_flood("看你多不一般", GID) == ""


def test_name_flood_needs_three_hits():
    w = _worker()
    now = time.time()
    others = [
        (300, "森森在宣示主權嗎"),
        (150, "森森等等私訊"),
        (120, "插不上話"),
        (110, "我不想上班了"),
        (100, "你們不要在群裡吵起來了"),
        (90, "湯汁紅紅亮亮的"),
        (80, "吃來吃去就吃一點那個味道"),
        (70, "看你多不一般"),
    ]
    w._recent_texts_by_group[GID] = [t for _a, t in others]
    w._recent_texts_at[GID] = [now - a for a, _t in others]
    assert w._named_target_flood("森森準備舔鈴鐺了", GID) == ""
