"""跨號同梗：前綴沒撞，也要能在句尾／句中看到同一個東西。

15:07:23 小小「風口浪尖我比較想吞熱騰騰的精液，小穴都濕了」、
15:07:31 小天後「重口味我不行，只收滾燙的精液」——八秒內兩號同一個名詞，
兩句開頭毫無交集，前綴三字比對一次都沒命中。
"""

from test_worker_reply_arbitration import MANAGED, _worker


def test_mid_sentence_noun_collision_is_caught():
    worker = _worker(sorted(MANAGED)[0])
    worker._cross_recent_items = lambda gid, sec: [
        ("重口味我不行，只收滾燙的精液", 8.0)
    ]
    hit = worker._cross_target_burst(
        "風口浪尖我比較想吞熱騰騰的精液，小穴都濕了", 1
    )
    assert "精液" in hit


def test_head_collision_still_wins():
    worker = _worker(sorted(MANAGED)[0])
    worker._cross_recent_items = lambda gid, sec: [
        ("和進出口，煮給我吃再床上見真章", 5.0)
    ]
    hit = worker._cross_target_burst("和進出口，不穿日料改把我剝光在流理台吃個夠", 1)
    assert "和進出" in hit or "進出口" in hit


def test_unrelated_sentences_do_not_collide():
    worker = _worker(sorted(MANAGED)[0])
    worker._cross_recent_items = lambda gid, sec: [("好, 我先去洗澡", 9.0)]
    assert worker._cross_target_burst("你敢不敢今晚來找我", 1) == ""


"""17:15:08/11 三秒內兩號同一個「大雞雞」：句中名詞撞車的真實案例。"""
from test_worker_reply_arbitration import MANAGED, _worker


def test_real_pair_from_1715_is_caught():
    w = _worker(sorted(MANAGED)[0])
    w._cross_recent_items = lambda g, s: [
        ("華發，那敢不敢拿大雞雞來餵飽我的小穴", 3.0)
    ]
    hit = w._cross_target_burst("不如留著大雞雞來把我幹到求饒", 1)
    assert "大雞雞" in hit


def test_same_opening_word_alone_is_not_enough():
    """只有「不如」這種虛詞開頭相同、內容不同時，仍可放行（不亂剪）。"""
    w = _worker(sorted(MANAGED)[0])
    w._cross_recent_items = lambda g, s: [("不如來請我喝下午茶比較實在吧", 25.0)]
    hit = w._cross_target_burst("床上功夫再說吧", 1)
    assert hit == ""
