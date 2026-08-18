import random

from app.persona import (
    BOY_PROACTIVE,
    CITY_SPOTS,
    DAILY_TOPICS,
    GIRL_PROACTIVE,
    SAFETY_TOPICS,
    SHOW_OFF_FEMALE,
    SHOW_OFF_MALE,
    TW_CITIES_MAJOR,
    generate_persona,
    generate_proactive_topic,
    get_system_prompt,
)

# 簡體專用字（繁體中文不會用這些字形）→ 繁體對應
SIMP_ONLY = {
    "个": "個", "这": "這", "为": "為", "专": "專", "么": "麼", "吗": "嗎",
    "诶": "欸", "请": "請", "谢": "謝", "问": "問", "间": "間", "门": "門",
    "开": "開", "关": "關", "东": "東", "车": "車", "马": "馬", "鱼": "魚",
    "鸟": "鳥", "风": "風", "飞": "飛", "电": "電", "云": "雲", "长": "長",
    "张": "張", "强": "強", "头": "頭", "见": "見", "观": "觀", "觉": "覺",
    "爱": "愛", "说": "說", "话": "話", "语": "語", "谁": "誰", "调": "調",
    "认": "認", "议": "議", "记": "記", "让": "讓", "该": "該", "过": "過",
    "还": "還", "来": "來", "对": "對", "点": "點", "写": "寫", "买": "買",
    "卖": "賣", "题": "題", "识": "識", "这": "這", "请": "請",
}

# 所有寫死的「會講出口」的群組文字池
CONTENT_POOLS = [DAILY_TOPICS, GIRL_PROACTIVE, BOY_PROACTIVE,
                 SHOW_OFF_FEMALE, SHOW_OFF_MALE, SAFETY_TOPICS]


def test_persona_fields():
    p = generate_persona()
    for key in ("name", "gender", "age", "city", "district", "industry",
                "university", "personality", "hobbies", "looking_for",
                "meetups_done", "schedule"):
        assert key in p, f"missing {key}"
    assert p["gender"] in ("男", "女")
    assert 21 <= p["age"] <= 34
    assert p["city"] in CITY_SPOTS
    assert 1 <= p["meetups_done"] <= 3


def test_persona_region_distribution():
    # 前 6 個水軍輪流佔據 6 個主要城市（全島覆蓋）
    used = []
    cities = []
    for _ in range(6):
        p = generate_persona(used)
        used.append(p["city"])
        cities.append(p["city"])
    assert len(set(cities)) == 6, cities


def test_system_prompt_content():
    p = generate_persona()
    sp = get_system_prompt(p)
    assert "繁體中文" in sp
    assert "1000 台幣" in sp
    assert "無安全審核" in sp
    assert "仙人跳" in sp  # 安全賣點話術
    assert p["city"] in sp
    assert p["name"] in sp


def test_no_simplified_chinese_in_content_pools():
    """語言硬規則：所有寫死的群組文字不得含簡體字"""
    for pool in CONTENT_POOLS:
        for s in pool:
            simp = [ch for ch in s if ch in SIMP_ONLY]
            assert not simp, f"簡體字 {simp} in: {s}"


def test_safety_topics_exist():
    """安全/把關閒聊池（設計比 10%）必須存在且非空"""
    assert len(SAFETY_TOPICS) >= 4
    assert any("仙人跳" in s for s in SAFETY_TOPICS)


def test_proactive_topic_distribution_has_safety():
    """主場發言 10% 是安全把關（抽樣驗證會抽到）"""
    p = generate_persona()
    seen = set()
    for _ in range(400):
        t = generate_proactive_topic(p)
        seen.add(t)
    # 400 次抽樣，安全池（6 句）應該至少被抽到一次
    assert any(t in SAFETY_TOPICS for t in seen), "安全話題從未出現"


def test_proactive_topic_not_empty():
    p = generate_persona()
    for _ in range(20):
        t = generate_proactive_topic(p)
        assert t and len(t) > 2
