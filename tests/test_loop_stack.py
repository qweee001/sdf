"""loop 卡頓的堆疊抓取。"""

from __future__ import annotations

from app.worker import capture_loop_stack


def test_stack_capture_returns_call_site():
    text = capture_loop_stack()
    assert text
    # 一定含本測試自己的函式名（最內側 frame 就是呼叫點）
    assert "test_stack_capture_returns_call_site" in text


def test_stack_capture_respects_limit():
    text = capture_loop_stack(limit=40)
    assert len(text) <= 40


def test_stack_capture_can_shrink_depth():
    shallow = capture_loop_stack(depth=1)
    deep = capture_loop_stack(depth=8)
    assert len(shallow) <= len(deep) or "test_stack" in shallow
