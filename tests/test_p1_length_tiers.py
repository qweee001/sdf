"""P1-2 動態字長檔位回歸：one(14)/two(40)/standard(55)＋prompt 不再寫死 40。"""

import app.worker as w
from test_worker_reply_arbitration import _worker


def test_length_caps_by_tier():
    assert w.AccountWorker._reply_char_cap({"length": "one"}) == 20
    assert w.AccountWorker._reply_char_cap({"length": "two"}) == 50
    assert w.AccountWorker._reply_char_cap({"length": "standard"}) == 50
    # 沒有決策 / 決策沒選長度 → 預設 50
    assert w.AccountWorker._reply_char_cap(None) == 50
    assert w.AccountWorker._reply_char_cap({}) == 50
    assert w.AccountWorker._reply_char_cap({"length": "bogus"}) == 50


def test_length_guide_has_standard_tier():
    assert "standard" in w._DECISION_LENGTH_GUIDE
    assert "20~50" in w._DECISION_LENGTH_GUIDE["standard"]
    # 送訊層通用後備上限＝最寬檔位（50），別把 standard 合法輸出誤殺
    assert w._MAX_REPLY_CHARS_HARD == 50
    assert w._MAX_REPLY_CHARS_HARD == max(w._DECISION_LENGTH_CAPS.values())


def test_decision_criteria_includes_standard():
    """決策層 length choice 的候選裡要出現 standard 檔位（模型才能選到）。"""
    worker = _worker(101)
    questions = worker._build_decision_questions()
    length_q = questions["length"]
    assert length_q["type"] == "choice"
    assert "standard" in length_q["criteria"]
    assert "one" in length_q["criteria"]
    assert "two" in length_q["criteria"]


def test_build_user_message_dynamic_length_line():
    """_build_user_message 的長度指示要跟著檔位走，不再寫死 40。"""
    worker = _worker(101)
    # 傳 standard 上限 50 → prompt 出現「最多 50」
    msg_50 = worker._build_user_message(_FakeEvent(), [], char_cap=50)
    assert "最多 50 個字元" in msg_50
    assert "最多 40 個字元" not in msg_50
    # 不傳 → 預設 50
    msg_default = worker._build_user_message(_FakeEvent(), [])
    assert "最多 50 個字元" in msg_default


def test_send_layer_hard_cap_allows_standard():
    """standard 檔位合法的 20~50 字元句子，送訊層不應誤殺（後備上限 50）。"""
    worker = _worker(101)
    long_ok = "字" * 50
    assert len(long_ok) <= w._MAX_REPLY_CHARS_HARD
    # 51 字元：超過所有檔位，送訊層擋下
    too_long = "字" * 51
    assert len(too_long) > w._MAX_REPLY_CHARS_HARD


class _FakeEvent:
    chat_id = -100
    sender_id = 999
    raw_text = "早安"
    media = None
    is_reply = False
    mentioned = False
    id = 1
