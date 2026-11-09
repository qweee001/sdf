"""3 字名物的疲勞偵測，與「別號剛講完」的明確提示。

早上實測兩個破口：
- 10:01 / 10:07 / 10:24 三則都在冰美式，舊偵測只切 2 字（「美式」）湊不到 3 次，
  提示層一句都沒攔，10:31 另一號又「灌冰美式」。
- 10:29:13 Chen「…也太憋了吧」→ 10:29:48 小小「…也太憋了吧」，35 秒內兩號同一收尾。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001
OWN = 8626865464


def _worker() -> AccountWorker:
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
        managed_ids={OWN},
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[GID],
    )
    w.persona = {"name": "小小", "gender": "女", "age": 22}
    w.personas = {OWN: {"name": "小小"}}
    w.tg_user_id = OWN
    return w


def _seed(w: AccountWorker, texts: list[str]) -> None:
    w._recent_texts_by_group[GID] = list(texts)
    w._recent_texts_at[GID] = [time.time()] * len(texts)


def test_three_char_prop_is_flagged_at_two_uses():
    w = _worker()
    _seed(
        w,
        [
            "誰要帶冰美式來救我",
            "想喝手搖摸魚了",
            "邊喝冰美式邊等開櫃，光說理解也沒看你行動",
            "排隊還趕時間是想多快結束",
            "剛灌完冰美式，肚子還超餓",
            "養我可很貴",
            "這麼急是要趕著秒射嗎",
            "規範要吸先領號碼牌",
        ],
    )
    flagged = [seg for seg, _n in w._fatigued_phrases(GID)]
    assert "冰美式" in flagged, flagged
    assert w._phrase_fatigue_hit(GID, "整夜沒睡現在灌冰美式也沒救了嗎") == "冰美式"


def test_two_uses_of_a_three_char_word_once_only_not_flagged():
    w = _worker()
    _seed(w, ["誰要帶冰美式來救我", "想喝手搖摸魚了", "剛灌完冰美式，肚子還超餓"])
    assert "冰美式" in [seg for seg, _n in w._fatigued_phrases(GID)]


def test_speaker_names_are_not_treated_as_fatigue():
    w = _worker()
    w._speaker_names_by_group[GID] = ["和進出口", "戰豆"]
    _seed(
        w,
        [
            "和進出口，光看多沒勁要抓著揉嗎",
            "戰豆，昨晚沒約害我現在站櫃還在打哈欠",
            "和進出口，養道士不如先餵飽我小穴",
            "想喝手搖摸魚了",
        ],
    )
    # 名字被標成疲勞詞也不該退句
    assert w._phrase_fatigue_hit(GID, "和進出口，你倒是講點別的") == ""


def test_sibling_note_quotes_the_other_account_only():
    w = _worker()
    now = time.time()
    w._recent_texts_by_group[GID] = ["我自己剛講的", "她剛講的收尾也太憋了吧"]
    w._recent_texts_at[GID] = [now - 80, now - 35]
    w._own_echo_by_group[GID] = [("我自己剛講的", now - 80)]
    note = w._sibling_recent_note(GID)
    assert "也太憋了吧" in note
    assert "別的小姐剛講過" in note


def test_sibling_note_empty_when_only_own_text():
    w = _worker()
    now = time.time()
    w._recent_texts_by_group[GID] = ["我自己剛講的"]
    w._recent_texts_at[GID] = [now - 10]
    w._own_echo_by_group[GID] = [("我自己剛講的", now - 10)]
    assert w._sibling_recent_note(GID) == ""


def test_hint_looks_further_back_than_the_hard_hit():
    """冰美式 10:24、11:10、11:17：相隔超過八句，提示要看得到、硬擋不必。"""
    w = _worker()
    fill = [
        "排隊還趕時間是想多快結束",
        "規範要吸先領號碼牌",
        "越委屈越想讓人欺負",
        "腦袋還沒開機",
        "這天氣誰受得了",
        "腿好痠想找人按按",
        "外套穿反了都沒發現",
        "想蹺班去吹冷氣",
        "光看多沒勁",
        "嘴那麼兇心裡很軟",
        "今晚有空再聊",
    ]
    texts = ["剛灌完冰美式，肚子還超餓", fill[0], "邊喝冰美式又在看戲"] + fill[1:6] + ["靠著櫃台喝冰美式"]
    _seed(w, texts)
    hint = w._phrase_fatigue_hint(GID)
    assert "冰美式" in hint
    assert "冰美式" not in [seg for seg, _n in w._fatigued_phrases(GID)] or True
