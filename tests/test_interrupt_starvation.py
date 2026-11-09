"""真人插話該不該砍掉手上這條：同一個人補話砍，路人閒聊在餓死場放行。

19:41–19:52（桃花源）四號連續二十分鐘零輸出，期間真人講了六十幾句——
和進出口與萬英尺每十五秒一句短打，生成永遠走不到「句子好了」就被下一個路人砍掉。
插話的場合，砍掉等於整場消失；只有同一個人自己又補話才真的會對不上話。
"""

from app.worker import AccountWorker

A = AccountWorker._interrupt_action


def test_fresh_task_holds():
    assert A(3.0, 40.0, False, False, 0.0) == "hold"


def test_same_sender_typing_again_cancels_early():
    assert A(20.0, 40.0, True, False, 0.0) == "cancel"


def test_same_sender_cancel_threshold_follows_budget():
    """慢場的門檻跟著預算走，不會十五秒就砍（那會砍掉正常生成）。"""
    assert A(20.0, 65.0, True, False, 0.0) == "hold"
    assert A(33.0, 65.0, True, False, 0.0) == "cancel"


def test_passers_by_over_budget_still_cancel_when_bots_are_active():
    assert A(70.0, 65.0, False, False, 30.0) == "cancel"


def test_starved_group_is_spared():
    assert A(70.0, 65.0, False, False, 700.0) == "hold"


def test_text_ready_never_cancelled_by_strangers():
    assert A(70.0, 65.0, False, True, 0.0) == "hold"


def test_hard_ceiling_still_wins():
    """硬上限還在，但句子已生成好／正卡在拒答逃生時放寬到 300 秒。"""
    assert A(200.0, 65.0, False, True, 900.0) == "hold"
    assert A(320.0, 65.0, False, True, 900.0) == "cancel"


def test_escape_chain_gets_longer_ceiling():
    """逃生是四階×備援模型的序列呼叫，180 秒必定砍在半路，等於白跑整條。"""
    assert A(200.0, 65.0, False, False, 0.0, in_escape=True) == "hold"
    assert A(320.0, 65.0, False, False, 0.0, in_escape=True) == "cancel"


def test_consecutive_cancel_breaker_stops_stranger_cancels():
    """連三條都被路人砍掉就換誰講都不砍，先讓一句活著落地。"""
    assert A(70.0, 65.0, False, False, 30.0, breaker=True) == "hold"
    # 同一個人自己補話仍然照砍（那條真的會答錯人）
    assert A(40.0, 65.0, True, False, 30.0, breaker=True) == "cancel"


def test_same_sender_still_cancels_even_when_starved():
    """他自己又補一句，答案就過期了，餓死場也照砍。"""
    assert A(40.0, 65.0, True, False, 700.0) == "cancel"
