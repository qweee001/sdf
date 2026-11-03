"""熱場回場：真人越兇、水軍越隱形是反的。

實測 11-03 21:41:06→21:55:54 有 14 分 49 秒完全沒有一則水軍，
期間 40 則男群友自己在比尺寸（最長連續 52 則），因為 hot_field 100% 關 proactive、
熱場又把普通回覆名額鎖死在 5 分鐘 2 則。這裡釘住三件事：
熱度縮放額度、極熱時縮短群級鎖、以及潛水過久的回場保底。
"""
import asyncio
import time

import pytest

from app.database import Database, hot_budget, hot_claim_window
from app.persona import get_system_prompt


def test_hot_budget_scales_with_density():
    assert hot_budget(0) == 2
    assert hot_budget(14) == 2
    assert hot_budget(20) == 3
    assert hot_budget(45) == 4
    assert hot_budget(200) == 5


def test_hot_claim_window_shrinks_when_very_hot():
    assert hot_claim_window(14) == 20
    assert hot_claim_window(29) == 20
    assert hot_claim_window(40) == 12


def _persona(**kw):
    base = {
        "name": "小小",
        "gender": "女",
        "age": 22,
        "city": "台北",
        "district": "士林",
        "industry": "金融業",
        "university": "高師大",
        "personality": "大膽風騷",
        "hobbies": ["美容"],
        "looking_for": "穩定的約會對象",
        "meetups_done": 3,
        "schedule": "排班",
        "chat_style": "生活碎念",
    }
    base.update(kw)
    return base


def test_young_account_does_not_call_herself_sister():
    prompt = get_system_prompt(_persona())
    assert "別自稱「姊姊」" in prompt
    assert "22" in prompt


def test_mature_account_may_call_herself_sister():
    prompt = get_system_prompt(_persona(name="Chen", age=38))
    assert "自稱「我」或「姊姊」都可以" in prompt
    assert "弟弟" in prompt


async def _seed(db, group_id, humans, *, span=250.0):
    now = time.time()
    for index in range(humans):
        await db._c.execute(
            "INSERT INTO group_events (group_id, message_id, sender_kind, observed_at) "
            "VALUES (?, ?, 'human', ?)",
            (group_id, 1000 + index, now - span + index),
        )
    await db._c.commit()


async def _claim_aged(db, group_id, message_id, age, account="a0"):
    await db._c.execute(
        "INSERT INTO reply_events (group_id, message_id, account_id, stage, reason, at) "
        "VALUES (?, ?, ?, 'claimed', 'human', ?)",
        (group_id, message_id, account, time.time() - age),
    )
    await db._c.commit()


async def _scenario(path, pressure_claims: int, last_sent_age: float | None, humans: int = 45):
    db = Database(str(path))
    await db.connect()
    await _seed(db, 777, humans)
    for index in range(pressure_claims):
        await _claim_aged(db, 777, 5000 + index, 60 + index)
    if last_sent_age is not None:
        await db._c.execute(
            "INSERT INTO reply_events (group_id, message_id, account_id, stage, reason, at) "
            "VALUES (777, 1, 'a0', 'sent', 'human', ?)",
            (time.time() - last_sent_age,),
        )
        await db._c.commit()
    admitted = await db.admit_ordinary_reply(777, 9000, "a1")
    await db.close()
    return admitted


def test_hot_group_admits_more_than_two_per_five_minutes(tmp_path):
    # 5 分鐘 45 則真人 → 額度 4，已經 2 則仍可再吃；舊版固定 2 會在這裡擋死
    assert asyncio.run(_scenario(tmp_path / "a.db", pressure_claims=2, last_sent_age=120)) is True


def test_return_to_field_after_long_silence_overrides_budget(tmp_path):
    # 額度已滿、20 秒鎖也踩著，但水軍已潛水 7 分鐘 → 必須強制回場
    assert asyncio.run(_scenario(tmp_path / "b.db", pressure_claims=5, last_sent_age=420)) is True


def test_short_silence_still_respects_budget(tmp_path):
    # 只潛水 2 分鐘，額度滿了就繼續讓路，不准搶麥
    assert asyncio.run(_scenario(tmp_path / "c.db", pressure_claims=5, last_sent_age=120)) is False


def test_ten_minute_claim_cap_still_applies(tmp_path):
    assert asyncio.run(_scenario(tmp_path / "d.db", pressure_claims=8, last_sent_age=30, humans=45)) is False
