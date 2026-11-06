"""骨架軸：整場同一個「引述某人＋逗號＋葷斷言」的模子（2026-11-05 20:54–21:42 實測）。

六則水軍的開頭字全不同（嘴／沒／專／連／森／多），起手詞那一軸全部放行，
群友看到的還是同一台機器：

  20:54 RICH「嘴上說怕，搞不好洗完澡趴在床上屁股翹最高的就是你」
  20:56 小天後「多吃點肉補體力，待會床上可別先喊投降」
  21:03 RICH「沒痔瘡講這麼順，後面是天天洗乾淨等著被頂嗎」
  21:27 RICH「專挑這時候表白裝深情，鈴鐺指尖輕輕碰一下就直接硬到發燙」
  21:32 RICH「連想被弄都能秒懂，默契好成這樣乾脆晚上抱在一起睡嘛」
  21:42 RICH「森森到底行不行，別人家洗好躺平你三秒就軟掉」

同時追兩條：這六則有五則完全沒講到自己（自曝免責照樣成立，因為 21:27 的「燙」
被當成她自己的身體），以及禁問禁過頭（21:03 之後五則全斷言，一小時只有一則問句）。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001

_QUOTE_ASSERTS = (
    "嘴上說怕，搞不好洗完澡趴在床上屁股翹最高的就是你",
    "專挑這時候表白裝深情，鈴鐺指尖輕輕碰一下就直接硬到發燙",
    "森森到底行不行，別人家洗好躺平你三秒就軟掉",
)
_QUESTION_TAIL = "沒痔瘡講這麼順，後面是天天洗乾淨等著被頂嗎"
_SELF_LINE = "偷約都不揪，我等等去酒吧喝茫讓人帶走"


def _worker(name: str = "RICH") -> AccountWorker:
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
    return w


def _seed(w: AccountWorker, pairs) -> None:
    """pairs＝[(幾秒前, 句子)]，由舊到新。"""
    now = time.time()
    w._recent_texts_by_group[GID] = [t for _age, t in pairs]
    w._recent_texts_at[GID] = [now - age for age, _t in pairs]


def test_quote_assert_shape_detected():
    w = _worker()
    for line in _QUOTE_ASSERTS:
        assert w._is_quote_assert(line), line


def test_self_line_is_not_the_shape():
    w = _worker()
    assert not w._is_quote_assert(_SELF_LINE)


def test_single_clause_is_not_the_shape():
    w = _worker()
    assert not w._is_quote_assert("傲嬌小狗欠調教是不是")


def test_question_tail_is_not_the_shape():
    """問句收尾的骨架也會被問句軸管，不重複算一次。"""
    w = _worker()
    assert not w._is_quote_assert("後面是天天洗乾淨等著被頂嗎")


# ---- 「燙」是他在燙：狀態字要看子句主詞 ----------------------------------


def test_state_word_on_another_body_is_not_self_disclosure():
    """21:27「…鈴鐺指尖輕輕碰一下就直接硬到發燙」的燙不是她的。

    舊版狀態字一律算自曝，這則同時被判成播報與自曝，兩道閘門互相抵銷。
    """
    w = _worker()
    line = "專挑這時候表白裝深情，鈴鐺指尖輕輕碰一下就直接硬到發燙"
    assert not w._has_self_disclosure(line)
    assert w._is_press_box(line)


def test_subjectless_own_state_still_counts():
    w = _worker()
    for line in ("冷氣太強腳都冰了", "腳痠到不想動", "剛敷完面膜躺下", "我在床上滾"):
        assert w._has_self_disclosure(line), line


# ---- 自曝免責只算真的自曝 ------------------------------------------------


def test_quote_asserts_after_one_self_line_still_ask_for_self():
    w = _worker()
    _seed(
        w,
        [
            (600, _SELF_LINE),
            (300, _QUOTE_ASSERTS[0]),
            (200, _QUOTE_ASSERTS[1]),
            (60, _QUOTE_ASSERTS[2]),
        ],
    )
    assert "沒人講到自己" in w._sentence_type_note([], GID, [])


# ---- 問句要得回來 --------------------------------------------------------


def test_three_assertions_in_a_row_ask_for_a_question():
    """連三則斷言才硬轉問句：門檻卡二會變 Q,A,A,Q,A,A 的節拍器。"""
    w = _worker()
    _seed(w, [(400, _QUOTE_ASSERTS[0]), (200, _QUOTE_ASSERTS[2]), (60, _SELF_LINE)])
    assert "問句收尾" in w._sentence_type_note([], GID, [])


def test_two_assertions_in_a_row_stay_quiet():
    w = _worker()
    _seed(w, [(200, _QUOTE_ASSERTS[2]), (60, _SELF_LINE)])
    assert "問句收尾" not in w._sentence_type_note([], GID, [])


def test_question_tail_is_allowed_when_nothing_asked_for_awhile():
    w = _worker()
    _seed(w, [(200, _QUOTE_ASSERTS[2]), (60, _SELF_LINE)])
    own = ["森森到底行不行，別人家洗好躺平你三秒就軟掉"]
    # 自己上一則不是問句 → 本來就不觸發「又是問句」；重點是連兩則斷言後放行。
    assert w._ask_tail_repeat(own, "這樣塞進來你撐得住嗎") is False
    assert w._local_veto("你先講，這樣塞進來你撐得住嗎", own, "", GID) == ""


def test_question_tail_still_blocked_right_after_a_question():
    w = _worker()
    _seed(w, [(200, "後面是天天洗乾淨等著被頂嗎")])
    own = ["後面是天天洗乾淨等著被頂嗎"]
    assert w._local_veto("你先講，這樣塞進來你撐得住嗎", own, "", GID)


# ---- 骨架本身也是硬軸 ----------------------------------------------------


def test_third_same_shape_is_vetoed():
    w = _worker()
    _seed(
        w,
        [
            (400, _QUOTE_ASSERTS[0]),
            (300, _QUOTE_ASSERTS[1]),
            (60, _QUOTE_ASSERTS[2]),
        ],
    )
    veto = w._local_veto(
        "沒交過女友還敢嘴，等等還不是被夾到一句話講不出來", [], "", GID
    )
    assert veto
    # 三則都是播報，播報密度那條比骨架那條先命中（理由更具體），兩者都算中。
    assert "引述" in veto or "講別人的事" in veto


def test_one_same_shape_is_not_yet_vetoed():
    w = _worker()
    _seed(w, [(60, _QUOTE_ASSERTS[2])])
    veto = w._local_veto("沒交過女友還敢嘴，等等還不是被夾到一句話講不出來", [], "", GID)
    assert veto == ""


def test_shape_hint_reaches_the_directive():
    w = _worker()
    _seed(w, [(400, _QUOTE_ASSERTS[0]), (60, _QUOTE_ASSERTS[2])])
    assert "別引述" in w._sentence_type_note([], GID, [])
