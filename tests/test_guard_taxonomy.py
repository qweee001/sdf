"""守門分層：硬結構與軟語感必須是互斥且完整的兩類。"""

from app import text_guard


def test_taxonomy_is_disjoint():
    assert text_guard.HARD_VIOLATIONS.isdisjoint(text_guard.SOFT_VIOLATIONS)


def test_hard_set_covers_structural_faults():
    for name in ("format_leak", "too_long", "too_short", "time_mismatch", "simplified", "gender_term"):
        assert name in text_guard.HARD_VIOLATIONS
    # 軟語感不准混進硬集合，否則「只軟才放行」的分流會失效
    for name in ("refusal", "own_echo", "too_flat", "broken_subject"):
        assert name not in text_guard.HARD_VIOLATIONS


def test_classify_violation_labels():
    assert text_guard.classify_violation(True) == "hard"
    assert text_guard.classify_violation(False) == "soft"
