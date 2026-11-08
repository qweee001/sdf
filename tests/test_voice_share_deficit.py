"""水軍佔比赤字：讓路機率要跟「群裡還剩多少自己人」連動。

11-08 15:07–16:14 那場，真人 3.0 則／分、四號合計 0.4 則／分（27／200＝13.5%），
而讓路的三道閘（尖峰 25%、熱場 40%、有人講話 55%）全是固定值：越熱的場水軍
越退越後面，最後整場只剩真人互嗆，看不出群裡有女生在。
"""

from test_worker_reply_arbitration import MANAGED, _worker  # noqa: E402


def test_share_counts_managed_by_role_and_id():
    rows = [
        {"role": "assistant", "sender_id": 999},
        {"role": "user", "sender_id": sorted(MANAGED)[0]},
        {"role": "user", "sender_id": 777},
        {"role": "user", "sender_id": 778},
    ]
    assert _worker(1)._voice_share(rows, MANAGED) == 0.5


def test_share_of_empty_window_is_zero():
    assert _worker(1)._voice_share([], MANAGED) == 0.0
    assert _worker(1)._voice_share(None, MANAGED) == 0.0


def test_deficit_is_full_when_nobody_of_us_spoke():
    """一句都沒有＝赤字 1，三道讓路機率全被壓到 0～最低。"""
    assert _worker(1)._share_deficit(0.0) == 1.0


def test_deficit_is_zero_once_target_is_reached():
    assert _worker(1)._share_deficit(0.30) == 0.0
    assert _worker(1)._share_deficit(0.62) == 0.0


def test_deficit_shrinks_proportionally_in_between():
    """13.5%（今天實測）→ 赤字約 0.55，三道機率各砍一半以上。"""
    deficit = _worker(1)._share_deficit(0.135)
    assert 0.5 < deficit < 0.6
