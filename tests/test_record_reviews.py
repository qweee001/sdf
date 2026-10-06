"""第②步的契約測試：審閱狀態寫進 DB（可稽核、跨裝置一致、跨帳號不重複計算）。

寫法照 tests/test_group_directory.py 的 harness：同步測試 + TestClient，
DB 操作用它回傳的 loop.run_until_complete。
"""

import time

from fastapi.testclient import TestClient

from test_group_directory import _insert_message, _login, _make_dashboard

GROUP = -5565520321
ACC_A = "acc-a"
ACC_B = "acc-b"


def _seed(db, loop, now):
    """同一則水軍訊息由兩個帳號各記一列（真實情況），加一則真人訊息。"""
    _insert_message(db, loop, ACC_A, GROUP, 111, "小小", "assistant", "嘴硬沒用", now - 300)
    _insert_message(db, loop, ACC_B, GROUP, 111, "小小", "assistant", "嘴硬沒用", now - 300)
    _insert_message(db, loop, ACC_A, GROUP, 111, "小小", "assistant", "待會來士林找我", now - 200)
    _insert_message(db, loop, ACC_A, GROUP, 999, "阿宏", "user", "人咧", now - 100)


def test_records_dedupe_across_accounts_and_count():
    """同一則訊息被兩個帳號記錄 → 紀錄只算一筆；真人的訊息不需檢視。"""
    client, db, manager, loop = _make_dashboard()
    with client:
        _login(client)
        _seed(db, loop, time.time())
        res = client.get(f"/api/groups/{GROUP}/records").json()
        bots = [r for r in res["records"] if r["needs_review"]]
        humans = [r for r in res["records"] if not r["needs_review"]]
        assert len(bots) == 2, [r["content"] for r in bots]
        assert len(humans) == 1
        assert res["counts"] == {"pending": 2, "reviewed": 0}, res["counts"]
        assert len({r["record_key"] for r in res["records"]}) == len(res["records"])


def test_review_is_persisted_and_shared():
    """標記後寫進 DB：換一個 client（模擬另一台裝置）讀到相同狀態。"""
    client, db, manager, loop = _make_dashboard()
    with client:
        _login(client)
        _seed(db, loop, time.time())
        first = client.get(f"/api/groups/{GROUP}/records").json()
        key = next(r["record_key"] for r in first["records"] if r["needs_review"])

        marked = client.post("/api/records/review", json={
            "group_id": GROUP, "record_key": key, "reviewed": True,
        }).json()
        assert marked["ok"] is True and marked["counts"] == {"pending": 1, "reviewed": 1}, marked

        other = TestClient(client.app)
        _login(other)
        again = other.get(f"/api/groups/{GROUP}/records").json()
        rec = next(r for r in again["records"] if r["record_key"] == key)
        assert rec["reviewed"] is True and rec["reviewed_at"]
        assert again["counts"] == {"pending": 1, "reviewed": 1}

        undo = client.post("/api/records/review", json={
            "group_id": GROUP, "record_key": key, "reviewed": False,
        }).json()
        assert undo["counts"] == {"pending": 2, "reviewed": 0}, undo


def test_review_requires_key_and_session():
    client, db, manager, loop = _make_dashboard()
    with client:
        assert client.get(f"/api/groups/{GROUP}/records").status_code == 401
        assert client.post("/api/records/review", json={"group_id": GROUP, "record_key": "x"}).status_code == 401
        _login(client)
        bad = client.post("/api/records/review", json={"group_id": GROUP})
        assert bad.status_code == 400
        assert "record_key" in bad.json()["error"]


def test_review_state_survives_new_messages():
    """標記之後又來新訊息，舊紀錄的審閱狀態不能被清掉。"""
    client, db, manager, loop = _make_dashboard()
    with client:
        _login(client)
        now = time.time()
        _insert_message(db, loop, ACC_A, GROUP, 111, "小小", "assistant", "第一則", now - 300)
        key = client.get(f"/api/groups/{GROUP}/records").json()["records"][0]["record_key"]
        client.post("/api/records/review", json={"group_id": GROUP, "record_key": key, "reviewed": True})
        _insert_message(db, loop, ACC_A, GROUP, 111, "小小", "assistant", "第二則", now - 10)

        res = client.get(f"/api/groups/{GROUP}/records").json()
        by_key = {r["record_key"]: r for r in res["records"]}
        assert by_key[key]["reviewed"] is True
        assert res["counts"] == {"pending": 1, "reviewed": 1}, res["counts"]


def test_review_survives_shorter_window():
    """同一則訊息在較短視窗也拿得到同一個 record_key（鍵不是流水號）。"""
    client, db, manager, loop = _make_dashboard()
    with client:
        _login(client)
        _seed(db, loop, time.time())
        wide = client.get(f"/api/groups/{GROUP}/records?limit=50").json()
        narrow = client.get(f"/api/groups/{GROUP}/records?limit=2").json()
        wide_keys = {r["record_key"] for r in wide["records"]}
        assert all(r["record_key"] in wide_keys for r in narrow["records"])
