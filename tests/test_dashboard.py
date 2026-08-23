import asyncio
import os
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import load_settings
from app.crypto import SecretBox
from app.dashboard import Dashboard
from app.database import Database
from app.manager import AccountManager
from app.telegram_login import TelegramLoginService

DB = "/tmp/sdf_test/dash_test.db"


def _make_dashboard():
    if os.path.exists(DB):
        os.remove(DB)
    s = load_settings()
    box = SecretBox(s.account_encryption_key)
    db = Database(DB)

    # 同一個 event loop 貫穿 db 整個生命週期，避免 aiosqlite 背景線程卡住退出
    loop = asyncio.new_event_loop()
    loop.run_until_complete(db.connect())

    manager = AccountManager(s, db, box)
    login = TelegramLoginService(s.tg_api_id, s.tg_api_hash)
    dash = Dashboard(s, manager, login)

    def _close():
        try:
            loop.run_until_complete(manager.aclose())
            loop.run_until_complete(db.close())
        finally:
            loop.close()

    client = TestClient(dash.app)

    def _exit(exc_type, exc, tb):
        try:
            return client.__exit__(exc_type, exc, tb)
        finally:
            _close()

    client.__exit__ = _exit
    return client


def test_health_public():
    with _make_dashboard() as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_login_flow():
    with _make_dashboard() as client:
        r = client.get("/api/status")
        assert r.status_code == 401
        r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401
        r = client.post("/api/login", json={"username": "admin", "password": "secret123"})
        assert r.status_code == 200
        r = client.get("/api/status")
        assert r.status_code == 200
        assert "accounts" in r.json()
        r = client.post("/api/logout")
        assert r.status_code == 200
        assert client.get("/api/status").status_code == 401


def test_login_rate_limit():
    with _make_dashboard() as client:
        for _ in range(11):
            client.post("/api/login", json={"username": "admin", "password": "bad"})
        r = client.post("/api/login", json={"username": "admin", "password": "secret123"})
        assert r.status_code == 429


def test_index_serves_zh_tw():
    with _make_dashboard() as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "zh-TW" in r.text
        assert "水軍控制台" in r.text


def test_groups_ui_uses_read_only_discovery_before_start():
    """群組設定畫面必須呼叫唯讀探索接口，不得要求先啟動互動。"""
    with _make_dashboard() as client:
        r = client.get("/")
        assert "/groups/available" in r.text
        assert "先啟動帳號，再回來勾選" not in r.text


def test_account_start_requires_login():
    with _make_dashboard() as client:
        assert client.post("/api/accounts/abc/start").status_code == 401
        assert client.delete("/api/accounts/abc").status_code == 401


def test_add_account_stays_stopped_until_setup(monkeypatch):
    """Telegram 登入完成只建立帳號，不得在設定人設與群組前啟動互動。"""
    start_calls = []

    async def fake_claim(self, auth_id):
        assert auth_id == "verified-auth"
        return SimpleNamespace(session_string="session", tg_user_id=123456)

    async def track_start(self, account_id):
        start_calls.append(account_id)
        return ""

    monkeypatch.setattr(TelegramLoginService, "claim", fake_claim)
    monkeypatch.setattr(AccountManager, "start", track_start)

    with _make_dashboard() as client:
        client.post("/api/login", json={"username": "admin", "password": "secret123"})
        r = client.post(
            "/api/accounts/add",
            json={"auth_id": "verified-auth", "name": "台北-美玲"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert start_calls == []

        account_id = r.json()["account"]["id"]
        status = client.get("/api/status").json()
        account = next(a for a in status["accounts"] if a["id"] == account_id)
        assert account["enabled"] is False
        assert account["is_running"] is False
        assert account["setup_complete"] is False


def test_account_apis_for_nonexistent_account_return_404():
    """不存在帳號時，start/stop/delete/privates 需回 404，不可誤回 200"""
    with _make_dashboard() as client:
        client.post("/api/login", json={"username": "admin", "password": "secret123"})
        assert client.post("/api/accounts/nope/start").status_code == 404
        assert client.post("/api/accounts/nope/stop").status_code == 404
        assert client.delete("/api/accounts/nope").status_code == 404
        assert client.get("/api/accounts/nope/privates").status_code == 404
        assert client.post("/api/accounts/nope/privates/1/read").status_code == 404


def test_persona_edit_and_groups_endpoints():
    """人設可編輯 + 指定群組 API（格式驗證 / 404 / 需登入）"""
    with _make_dashboard() as client:
        client.post("/api/login", json={"username": "admin", "password": "secret123"})
        # 不存在的帳號 → 404
        r = client.post("/api/accounts/nope/persona",
                        json={"persona": {"name": "阿美", "personality": "活潑"}})
        assert r.status_code == 404
        r = client.post("/api/accounts/nope/groups", json={"groups": [1, 2]})
        assert r.status_code == 404
        # 格式錯誤（缺名字 / 非 list）→ 400（格式檢查優先於存在性）
        r = client.post("/api/accounts/nope/persona", json={"persona": {}})
        assert r.status_code == 400
        r = client.post("/api/accounts/nope/groups", json={"groups": "abc"})
        assert r.status_code == 400
        # 登出後 → 401
        client.post("/api/logout")
        assert client.post("/api/accounts/x/persona",
                           json={"persona": {"name": "x"}}).status_code == 401
        assert client.post("/api/accounts/x/groups",
                           json={"groups": []}).status_code == 401


def test_status_includes_groups_fields():
    """/api/status 回傳含 groups 與 groups_available 欄位"""
    with _make_dashboard() as client:
        client.post("/api/login", json={"username": "admin", "password": "secret123"})
        r = client.get("/api/status")
        assert r.status_code == 200
        assert "accounts" in r.json()


def test_available_groups_endpoint_discovers_without_starting(monkeypatch):
    """停止中的帳號可用唯讀連線取得群組，無須先啟動互動 worker。"""
    async def fake_list(self, account_id):
        assert account_id == "stopped-account"
        return [{"id": -1001, "title": "台北交友群"}], ""

    monkeypatch.setattr(AccountManager, "list_available_groups", fake_list, raising=False)

    with _make_dashboard() as client:
        assert client.get(
            "/api/accounts/stopped-account/groups/available"
        ).status_code == 401
        client.post("/api/login", json={"username": "admin", "password": "secret123"})
        r = client.get("/api/accounts/stopped-account/groups/available")
        assert r.status_code == 200
        assert r.json() == {"groups": [{"id": -1001, "title": "台北交友群"}]}
