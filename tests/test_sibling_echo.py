"""發送前的水軍平行撞句：生成前看不到對方，只有發送這一道攔得到。

15:45:56 小天後「手勁夠大嗎 按哪裡呀」→ 15:46:14 同一號「站櫃腿超痠，手勁夠大再來按」：
兩條平行生成、落地只差十八秒，群友看到的是自己複讀自己。
只認九十秒內的落地句，冷掉的舊梗交給生成前的本機自檢。
"""

import asyncio
import time

from test_worker_reply_arbitration import MANAGED, _worker  # noqa: E402


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def get_group_messages(self, group_id, limit=8):
        return list(self.rows)


def _row(text, age=20.0, role="assistant"):
    return {"content": text, "timestamp": time.time() - age, "role": role}


def test_same_opening_within_window_is_caught():
    w = _worker(sorted(MANAGED)[0])
    w.db = _Db([_row("手勁夠大嗎 按哪裡呀")])
    assert asyncio.run(w._sibling_echo_recent(-1001, "站櫃腿超痠，手勁夠大再來按"))


def test_verbatim_four_chars_is_caught():
    w = _worker(sorted(MANAGED)[0])
    w.db = _Db([_row("進來坐一下嘛 沙發還空著")])
    assert asyncio.run(w._sibling_echo_recent(-1001, "今天好熱 進來坐一下嘛"))


def test_old_line_is_released_to_the_pre_generation_check():
    w = _worker(sorted(MANAGED)[0])
    w.db = _Db([_row("手勁夠大嗎 按哪裡呀", age=300.0)])
    assert not asyncio.run(
        w._sibling_echo_recent(-1001, "站櫃腿超痠，手勁夠大再來按")
    )


def test_human_lines_do_not_block():
    w = _worker(sorted(MANAGED)[0])
    w.db = _Db([_row("手勁夠大嗎", role="user")])
    assert not asyncio.run(w._sibling_echo_recent(-1001, "手勁夠大再來按"))


def test_unrelated_line_passes():
    w = _worker(sorted(MANAGED)[0])
    w.db = _Db([_row("腿正痠快來舔我小穴")])
    assert not asyncio.run(w._sibling_echo_recent(-1001, "黃金看多粗才值錢嘛"))
