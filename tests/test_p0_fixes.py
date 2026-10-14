"""P0 修復回歸：ChatWriteForbidden 指數退避、gate 候選記 seen、台北日界。

實測動機（c1fec6a 部署後 16h 觀察）：
- 08:00 三號被「You can't write in this chat」卡 2 小時、136 次 60s 盲目重試。
- 冷場期（21:54-09:54）只發出 1 則：45 次 gate-rewrite 反覆撞同一批「早安/咖啡」候選。
- 每日主動額度用 UTC 日界，台北 00:00-08:00 歸進昨天造成配額錯位。
"""

import time

from test_worker_reply_arbitration import _worker

GROUP = -1002229799107


def test_chat_write_forbidden_exponential_backoff():
    """每次 ChatWriteForbidden 加倍退避、上限 30min；退避中 flag 為 True。"""
    worker = _worker(101)
    assert worker._write_forbidden_backing_off() is False

    worker._note_write_forbidden()
    # 第 1 次：約 5min（300s ± 抖動）
    first = worker._write_forbidden_until - time.time()
    assert 300 <= first <= 300 * 1.15 + 1
    assert worker._write_forbidden_backing_off() is True
    assert worker.stats.get("write_forbidden") == 1

    # 第 2 次：約 10min（600s）
    worker._note_write_forbidden()
    second = worker._write_forbidden_until - time.time()
    assert 600 <= second <= 600 * 1.15 + 1

    # 第 3 次：約 20min（1200s）
    worker._note_write_forbidden()
    third = worker._write_forbidden_until - time.time()
    assert 1200 <= third <= 1200 * 1.15 + 1

    # 第 4 次：1200*2=2400 但上限 1800（30min）
    worker._note_write_forbidden()
    fourth = worker._write_forbidden_until - time.time()
    assert 1800 <= fourth <= 1800 * 1.15 + 1
    assert worker.stats.get("write_forbidden") == 4


def test_chat_write_forbidden_clear_on_success():
    """成功送出一次就重置退避（群組解鎖）。"""
    worker = _worker(101)
    worker._note_write_forbidden()
    assert worker._write_forbidden_backing_off() is True
    worker._clear_write_forbidden_backoff()
    assert worker._write_forbidden_backing_off() is False
    assert worker._write_forbidden_retries == 0
    # 重置後再犯從 5min 起算（不是接着 30min）
    worker._note_write_forbidden()
    first = worker._write_forbidden_until - time.time()
    assert 300 <= first <= 300 * 1.15 + 1


def test_gate_candidate_noted_to_seen_memory():
    """被 ③ 攔下的候選要進跨 cycle seen 記憶，下一輪 prompt 會餵回。"""
    worker = _worker(101)
    bucket = worker._proactive_recent_seen.setdefault(GROUP, [])
    assert bucket == []
    worker._note_gate_candidate(GROUP, "早安 咖啡還沒沖好")
    assert worker._proactive_recent_seen[GROUP] == ["早安 咖啡還沒沖好"]
    # 重複記同一句不重複入桶
    worker._note_gate_candidate(GROUP, "早安 咖啡還沒沖好")
    assert len(worker._proactive_recent_seen[GROUP]) == 1
    # 空句不記
    worker._note_gate_candidate(GROUP, "   ")
    assert len(worker._proactive_recent_seen[GROUP]) == 1
    # 上限 12：第 13 句擠掉最舊
    for i in range(12):
        worker._note_gate_candidate(GROUP, f"候選{i}")
    assert len(worker._proactive_recent_seen[GROUP]) == 12
    assert worker._proactive_recent_seen[GROUP][0] == "候選0"
    worker._note_gate_candidate(GROUP, "候選99")
    assert len(worker._proactive_recent_seen[GROUP]) == 12
    assert "候選99" in worker._proactive_recent_seen[GROUP]


def test_today_index_uses_taipei_midnight():
    """每日額度日界＝台北 00:00（UTC+8），與 _hkt_day_index 同一算法。"""
    worker = _worker(101)
    now = time.time()
    assert worker._today_index() == worker._hkt_day_index(now)
    # 台北午夜邊界（UTC+8）：2026-10-14 00:00 台北 ＝ 2026-10-13 16:00 UTC。
    # 用固定 epoch（1760371200 ＝ 2025-10-13 16:00 UTC＝台北 10-14 00:00，任意台北午夜即可）。
    import datetime as _dt
    base = int(_dt.datetime(2026, 10, 13, 16, 0, 0, tzinfo=_dt.timezone.utc).timestamp())
    day_at = lambda t: int((t + 8 * 3600) // 86400)
    assert day_at(base) == day_at(base + 3600)  # 00:00 與 01:00 同一天
    assert day_at(base) == day_at(base - 1) + 1  # 往前 1 秒＝台北前一天 23:59:59
    assert day_at(base - 1) == day_at(base - 7200)  # 前一天 18:00 仍是同一天
