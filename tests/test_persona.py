from app.persona import (
    CITY_SPOTS,
    TW_CITIES_MAJOR,
    generate_persona,
    generate_proactive_topic,
    get_system_prompt,
)


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
    assert p["city"] in sp
    assert p["name"] in sp


def test_proactive_topic_not_empty():
    p = generate_persona()
    for _ in range(20):
        t = generate_proactive_topic(p)
        assert t and len(t) > 2
