"""同秒生成撞開場：送出前跟別號剛發的比一次，撞到就剪掉重複的頭。"""

import time

from app import worker as worker_mod

MANAGED = {"name2id"}


def _w():
    w = object.__new__(worker_mod.AccountWorker)
    w.name = "小天後"
    w._counters = {}
    w._pcount = lambda key: w._counters.__setitem__(key, w._counters.get(key, 0) + 1)
    return w


def _row(text, ago=4.0):
    return {"role": "assistant", "content": text, "timestamp": time.time() - ago}


def test_same_opener_within_seconds_gets_trimmed_to_tail():
    w = _w()
    rows = [_row("掛號不用包紅包，直接包養我塞滿小穴比較實在")]
    out = w._declick_cross_opening("掛號不用包紅包，直接包晚上的房間給我比較實際", rows)
    assert out == "直接包晚上的房間給我比較實際"
    assert w._counters["declick_opening"] == 1


def test_old_or_different_opener_is_kept():
    w = _w()
    line = "掛號不用包紅包，直接包晚上的房間給我比較實際"
    assert w._declick_cross_opening(line, [_row("掛號不用包紅包，直接包養我", ago=90)]) == line
    assert w._declick_cross_opening(line, [_row("平平都嘛差不多，直接包養我")]) == line
    assert "declick_opening" not in w._counters


def test_human_rows_do_not_count():
    w = _w()
    line = "掛號不用包紅包，直接包晚上的房間給我比較實際"
    rows = [{"role": "user", "content": "掛號不用包紅包，直接包養我塞滿小穴", "timestamp": time.time()}]
    assert w._declick_cross_opening(line, rows) == line
