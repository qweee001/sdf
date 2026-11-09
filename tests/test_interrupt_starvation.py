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


def test_text_ready_same_sender_holds_too():
    """句子已經進到「只剩檢查與發送」，本人再補一句也不砍。

    熱場裡目標群友十幾秒就一句，砍了等於這一則永遠沒人回（claimed 56／sent 17）。
    正確性交給發送前的 _context_still_fresh。
    """
    assert A(20.0, 40.0, True, True, 0.0) == "hold"
    assert A(40.0, 65.0, True, True, 700.0) == "hold"


def test_hard_ceiling_still_wins():
    """硬上限還在，但句子已生成好／正卡在拒答逃生時放寬到 300 秒。"""
    assert A(200.0, 65.0, False, True, 900.0) == "hold"
    assert A(320.0, 65.0, False, True, 900.0) == "cancel"


def test_escape_chain_gets_longer_ceiling():
    """逃生是四階×備援模型的序列呼叫，180 秒必定砍在半路，等於白跑整條。"""
    assert A(200.0, 65.0, False, False, 0.0, in_escape=True) == "hold"
    assert A(320.0, 65.0, False, False, 0.0, in_escape=True) == "cancel"


def test_generation_in_flight_survives_same_sender_again():
    """已經在跑模型就不砍：15 秒門檻在熱場等於「有在生成就必砍」。

    21:00–21:30 認領 63 只送出 18，39 條全死在 human_interrupt；砍掉的都是
    正在燒 token 的那條，砍了還一併放掉 claim。排隊中才快砍（省一次生成）。
    """
    assert A(20.0, 40.0, True, False, 0.0, gen_started=True) == "hold"
    assert A(60.0, 65.0, True, False, 0.0, gen_started=True) == "hold"
    assert A(100.0, 65.0, True, False, 0.0, gen_started=True) == "cancel"
    # 路人插話在生成中也只能等到硬上限
    assert A(100.0, 65.0, False, False, 30.0, gen_started=True) == "hold"


def test_consecutive_cancel_breaker_stops_stranger_cancels():
    """連三條都被路人砍掉就換誰講都不砍，先讓一句活著落地。"""
    assert A(70.0, 65.0, False, False, 30.0, breaker=True) == "hold"
    # 同一個人自己補話仍然照砍（那條真的會答錯人）
    assert A(40.0, 65.0, True, False, 30.0, breaker=True) == "cancel"


def test_same_sender_still_cancels_even_when_starved():
    """他自己又補一句，答案就過期了，餓死場也照砍。"""
    assert A(40.0, 65.0, True, False, 700.0) == "cancel"
