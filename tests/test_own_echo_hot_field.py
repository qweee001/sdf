"""同號近義複讀硬擋＋熱場關 proactive-join。

實測（桃花源 -1002229799107 14:28–15:16）：小小 10 分鐘內把
「幹，光講舌頭就讓我濕了」又講成「幹光講就濕了」；phrase_fatigue 只盯
句末 2 字，攔不住這對。熱場時 proactive-join 還在自講。
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

from app.text_guard import is_own_echo, own_echo_key
from test_worker_reply_arbitration import _worker


GROUP = -1002229799107


def test_own_echo_key_strips_gan_punct_space():
    a = own_echo_key("幹，光講舌頭就讓我濕了")
    b = own_echo_key("幹光講就濕了")
    assert "幹" not in a and "幹" not in b
    assert "光講" in a and "濕了" in a
    assert "光講" in b and "濕了" in b


def test_own_echo_catches_wet_variant():
    """正規化後「光講…濕了」10 分鐘內第二句命中。"""
    first = "幹，光講舌頭就讓我濕了"
    second = "幹光講就濕了"
    assert is_own_echo(second, [first])
    assert is_own_echo(first, [second])
    assert not is_own_echo("今晚想出門晃晃", [first])


def test_own_echo_drops_reply_without_generating_second_pass_if_retry_also_echoes():
    async def main():
        worker = _worker(101)
        worker._record_sent_text(GROUP, "幹，光講舌頭就讓我濕了")
        worker._call_ai = AsyncMock(side_effect=[
            "幹光講就濕了",
        ])
        event = AsyncMock(
            sender_id=999,
            chat_id=GROUP,
            id=41,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="舌頭那個",
            media=None,
        )
        event.sender = None
        text = await worker._generate_reply(event)
        assert text == ""
        assert worker._call_ai.await_count == 1, "命中近義複讀必須直接 drop，不得二次重寫生成"
        reason = worker._generation_reasons.get(worker._generation_key(event))
        assert reason == "own_echo"

    asyncio.run(main())


def test_own_echo_expires_after_ten_minutes():
    worker = _worker(101)
    worker._record_sent_text(GROUP, "幹，光講舌頭就讓我濕了")
    worker._own_echo_by_group[GROUP][0] = (
        worker._own_echo_by_group[GROUP][0][0],
        time.time() - 601,
    )
    assert worker._own_echo_recent(GROUP) == []
    assert not worker._is_own_echo("幹光講就濕了", worker._own_echo_recent(GROUP))


def test_proactive_drops_own_echo():
    async def main():
        worker = _worker(101)
        worker._record_sent_text(GROUP, "幹，光講舌頭就讓我濕了")

        class _TopicDB:
            async def get_group_messages(self, group_id, limit=100):
                return [
                    {
                        "sender_name": "風口浪尖",
                        "sender_id": 5,
                        "role": "user",
                        "content": "今晚有人嗎",
                        "timestamp": time.time(),
                    }
                ]

            async def get_group_shared_notes(self, group_id, account_id, limit=10):
                return []

            async def recent_bot_texts_by_group(self, group_id):
                return []

        worker.db = _TopicDB()
        worker._call_ai = AsyncMock(return_value="幹光講就濕了")
        topic = await worker._generate_context_topic(GROUP)
        assert topic == ""
        assert worker._call_ai.await_count == 1, "主動發言命中 own_echo 必須直接 drop，不得在 3 次迴圈中再次生成"

    asyncio.run(main())


def test_female_persona_blocks_male_genital_claims_with_spaces():
    from app.text_guard import gender_term_violation

    female = {"gender": "女"}
    assert gender_term_violation(female, "我 雞雞")
    assert gender_term_violation(female, "割 我 雞 雞")
    assert gender_term_violation(female, "我 的 屌")
    assert gender_term_violation(female, "想 割 我 雞雞")



def test_hot_field_true_when_three_humans_in_one_minute():
    worker = _worker(101)
    now = time.time()
    worker._recent_human_ts_by_group[GROUP] = [now - 50, now - 20, now - 5]
    assert worker._hot_field(GROUP) is True


def test_hot_field_false_when_sparse():
    worker = _worker(101)
    now = time.time()
    worker._recent_human_ts_by_group[GROUP] = [now - 50, now - 5]
    assert worker._hot_field(GROUP) is False
    worker._recent_human_ts_by_group[GROUP] = [now - 90, now - 80, now - 70]
    assert worker._hot_field(GROUP) is False


def test_female_persona_blocks_male_genital_claims():
    from app.text_guard import gender_term_violation

    female = {"gender": "女"}
    male = {"gender": "男"}
    assert gender_term_violation(female, "幹嘛想割我雞雞")
    assert gender_term_violation(female, "想割我雞雞")
    assert gender_term_violation(female, "我的屌很大")
    assert gender_term_violation(female, "割我的老二")
    assert not gender_term_violation(male, "幹嘛想割我雞雞")


def test_allowed_active_hours_10_to_22():
    worker = _worker(101)
    with patch.object(worker, "_taipei_hour", return_value=10.0):
        assert worker._is_allowed_active_hour() is True
        assert worker._is_sleeping() is False

    with patch.object(worker, "_taipei_hour", return_value=21.99):
        assert worker._is_allowed_active_hour() is True
        assert worker._is_sleeping() is False

    with patch.object(worker, "_taipei_hour", return_value=22.0):
        assert worker._is_allowed_active_hour() is False
        assert worker._is_sleeping() is True

    with patch.object(worker, "_taipei_hour", return_value=9.99):
        assert worker._is_allowed_active_hour() is False
        assert worker._is_sleeping() is True

    with patch.object(worker, "_taipei_hour", return_value=3.0):
        assert worker._is_allowed_active_hour() is False
        assert worker._is_sleeping() is True



def test_illogical_sentence_violation():
    from app.text_guard import illogical_sentence_violation

    # 實測病句與常識翻車案例
    assert illogical_sentence_violation("嘴長誰不會")
    assert illogical_sentence_violation("哥哥想炒哪種烤肉")
    assert illogical_sentence_violation("炒烤肉是怎樣")
    assert illogical_sentence_violation("炒生魚片")
    assert illogical_sentence_violation("烤稀飯")
    assert illogical_sentence_violation("煮烤肉")
    assert illogical_sentence_violation("煎烤肉")
    assert illogical_sentence_violation("光長嘴誰")
    assert illogical_sentence_violation("誰不會")

    # 正常口語應放行
    assert not illogical_sentence_violation("出一張嘴誰不會")
    assert not illogical_sentence_violation("只會出一張嘴喔")
    assert not illogical_sentence_violation("想吃哪種烤肉")
    assert not illogical_sentence_violation("這家烤肉超推")
    assert not illogical_sentence_violation("烤肉要不要一起去")
    assert not illogical_sentence_violation("生魚片超新鮮")


def test_approved_vision_models_allows_agnes():
    from app.media import APPROVED_VISION_MODELS
    assert "agnes-3.0-flash" in APPROVED_VISION_MODELS
    assert "gemini-3.5-flash-lite" in APPROVED_VISION_MODELS



