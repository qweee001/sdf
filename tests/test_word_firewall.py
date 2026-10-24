"""零延遲用字防火牆（instant_word_fix / needs_model_review）測試。"""

from app.text_guard import (
    instant_word_fix,
    needs_model_review,
)


def test_instant_word_fix_fixes_variant_chars():
    # 港式古體字 → 台灣教育部標準字
    fixed, changed = instant_word_fix("在牀上等妳")
    assert changed is True
    assert fixed == "在床上等妳"

    fixed, changed = instant_word_fix("喫飯了沒")
    assert changed is True
    assert fixed == "吃飯了沒"


def test_instant_word_fix_fixes_common_typos():
    fixed, changed = instant_word_fix("你因該早點睡")
    assert changed is True
    assert "應該" in fixed

    fixed, changed = instant_word_fix("我以經到了")
    assert changed is True
    assert "已經" in fixed


def test_instant_word_fix_fixes_mainland_terms():
    fixed, changed = instant_word_fix("叫個外賣吃")
    assert changed is True
    assert "外送" in fixed

    fixed, changed = instant_word_fix("去搭地鐵")
    assert changed is True
    assert "捷運" in fixed


def test_needs_model_review_skips_common_text():
    # 日常口語、露骨詞、常用字 → 不需要叫模型
    assert needs_model_review("今天加班好累喔") is False
    assert needs_model_review("要不要來我家抱著睡") is False
    assert needs_model_review("被按在床上抓床單") is False
    assert needs_model_review("光會出一張嘴") is False


def test_needs_model_review_flags_rare_characters():
    # 真正的罕見字（不在語料字集內）才升級給模型審核
    assert needs_model_review("這是一個氼字") is True
    assert needs_model_review("龘靐") is True
