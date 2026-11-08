"""字數上限硬剪要剪在詞界，不留孤字尾。"""

from app.worker import AccountWorker


def test_cut_before_clause_start_not_mid_word():
    # 21:57 實例：8/12 字上限時剪在「狠」後面，留下「腿間狠」這種殘尾。
    got = AccountWorker._trim_to_cap("深情森森想被我夾在腿間狠狠夾住", 12)
    assert got == "深情森森想被我夾在腿間"


def test_cut_before_yao_after_name():
    # 21:44 實例：「方sir不能舔難道要」→ 尾巴那個「要」是下一句的起頭。
    got = AccountWorker._trim_to_cap("方sir不能舔難道要一直舔", 8)
    assert got == "方sir不能舔難道"


def test_cut_before_bei():
    got = AccountWorker._trim_to_cap("我剛洗完澡躺著超想被塞好塞滿", 12)
    assert got == "我剛洗完澡躺著超想"


def test_punctuation_still_preferred():
    got = AccountWorker._trim_to_cap("點完香氛蠟燭躺上床，沒生病就過來抱緊", 12)
    assert got == "點完香氛蠟燭躺上床"


def test_dangling_tail_stripped():
    assert AccountWorker._strip_dangling("戰豆嘴那麼兇心", 6) == "戰豆嘴那麼兇心"
    assert AccountWorker._strip_dangling("那個很兇的", 4) == "那個很兇"
    assert AccountWorker._strip_dangling("很兇的話", 2) == "很兇"


def test_floor_respected_when_no_boundary():
    # 找不到詞界就維持硬剪，不會剪到比地板還短。
    got = AccountWorker._trim_to_cap("一二三四五六七八九十甲乙丙", 8)
    assert len(got) == 8
