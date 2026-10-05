"""③ 審核校準：測資格式 + 指標計算（不連網，離線可跑）。

誤放行率與誤攔截率是調整 DECISION_GATE_THRESHOLD 的唯一依據；
這裡鎖住「測資合法」與「指標算對」兩件事，避免量出來的數字本身是錯的。
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import jev_calibrate as cal  # noqa: E402


def test_cases_file_is_well_formed():
    cases = cal.load_cases()
    assert len(cases) >= 14, "標註測資太少，量不出誤放行/誤攔截"
    tags = {c["id"] for c in cases}
    # 覆蓋面：該放行的、該擋下的、以及每一種 issue 都要有
    assert any(c["expect_send"] for c in cases)
    for issue in ("offtopic", "contradict", "fabricate", "repeat", "tone", "time", "simplified"):
        assert any(c["expect_issue"] == issue for c in cases), f"缺 {issue} 案例"
    assert tags


def test_cases_file_rejects_bad_rows(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps({"id": "x", "context": "c", "text": "t", "expect_issue": "nope", "expect_send": True}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        cal.load_cases(bad)


def test_summarize_counts_false_allow_and_false_block():
    rows = [
        {"expect_send": True, "got_send": True, "expect_issue": "none", "got_issue": "none", "latency": 1.0},
        {"expect_send": True, "got_send": False, "expect_issue": "none", "got_issue": "tone", "latency": 1.0},
        {"expect_send": False, "got_send": True, "expect_issue": "offtopic", "got_issue": "none", "latency": 1.0},
        {"expect_send": False, "got_send": False, "expect_issue": "repeat", "got_issue": "repeat", "latency": 1.0},
    ]
    stats = cal.summarize(rows, 0.5)
    assert stats["false_allow"] == 1
    assert stats["false_block"] == 1
    assert stats["false_allow_rate"] == pytest.approx(0.25)
    assert stats["false_block_rate"] == pytest.approx(0.25)
    assert stats["accuracy"] == pytest.approx(0.5)
    # issue 只有第 1、4 例判對（第 2 例把 none 判成 tone、第 3 例把 offtopic 判成 none）
    assert stats["issue_accuracy"] == pytest.approx(0.5)


def test_summarize_handles_empty_input():
    stats = cal.summarize([], 0.5)
    assert stats["n"] == 0
    assert stats["false_allow_rate"] == 0.0


def test_live_run_requires_key(monkeypatch):
    monkeypatch.delenv("DECISION_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        cal.asyncio.run(cal.run_live([], 0.5, "jev-latest"))
