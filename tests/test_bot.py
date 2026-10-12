"""Telegram 控制台 bot 橋接的測試。"""

import asyncio
import time

import pytest

from app.bot import TgControlBot, make_bot
from app.config import load_settings


def _settings(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "8969553283:AAFUxP2Y4duPj-7cT--8vfO77iLbdNkxY5s")
    monkeypatch.setenv("BOT_ADMIN_IDS", "111, 222")
    return load_settings()


class _FakeDB:
    def __init__(self):
        self.messages = [
            {
                "role": "user",
                "sender_id": 5,
                "sender_name": "阿宏",
                "content": "有人在嗎",
                "timestamp": 1760000000.0,
            },
            {
                "role": "assistant",
                "sender_id": 99,
                "sender_name": "小小",
                "content": "有啊哥哥",
                "timestamp": 1760000010.0,
            },
        ]
        self.overview = [
            {
                "group_id": -100,
                "msg_count": 30,
                "last_ts": 1760000000.0,
                "last_human_ts": 1760000000.0,
                "human_senders": 3,
            }
        ]
        self.labels = {-100: "桃花源"}
        self.privates = [
            {
                "sender_id": 7,
                "sender_name": "阿宏",
                "content": "加我LINE",
                "timestamp": 1760000000.0,
                "read": 0,
            }
        ]

    async def get_group_messages(self, gid, limit=100):
        assert gid == -100
        return list(self.messages)[:limit]

    async def group_overview(self, exclude_senders=()):
        return list(self.overview)

    async def get_group_labels(self):
        return dict(self.labels)

    async def list_accounts(self):
        return [{"id": "a1", "name": "台北-小小"}]

    async def get_private_messages(self, account_id, limit=50):
        assert account_id == "a1"
        return list(self.privates)[:limit]

    async def update_account(self, account_id, **k):
        return {"id": account_id, "updated": True}

    async def ai_call_summary(self, hours=24):
        return {
            "by_purpose": {
                "reply": {"calls": 12, "retries": 1, "measured": 10,
                          "prompt_tokens": 5000, "completion_tokens": 800,
                          "avg_latency_ms": 1500.0},
                "proactive": {"calls": 5, "retries": 0, "measured": 5,
                              "prompt_tokens": 2000, "completion_tokens": 300,
                              "avg_latency_ms": 1200.0},
            },
            "totals": {
                "calls": 17, "retries": 1, "measured": 15,
                "prompt_tokens": 7000, "completion_tokens": 1100,
                "avg_latency_ms": 1411.8,
            },
        }


class _FakeManager:
    def __init__(self):
        self.db = _FakeDB()
        self.workers = {
            "a1": type("W", (), {"is_running": True})(),
        }
        self.calls = []
        self._media = True
        self._gen = True
        self._voice = False
        self.config = type(
            "Cfg",
            (),
            {
                "ai_model": "glm-5.3",
                "chat_base_url": "https://ollama.com/v1",
                "ai_fallback_models": ("kimi-k3",),
                "vision_model": "glm-5.3-flash",
                "vision_fallback_model": "deepseek-v4.1-flash",
                "image_model": "google/imagen-4.0-fast-generate-001",
                "video_model": "minimax/minimax-h3",
                "speech_model": "openai/tts-1",
            },
        )()
        self._saved_groups: dict[str, list] = {}

    async def status(self):
        return {
            "running": 3,
            "total": 3,
            "reply_audit": {
                "sent": {"ok": 10},
                "policy": {"near_duplicate": 4, "gate_held": 2},
            },
            "media_spend_usd": 0.4,
            "media_budget_usd": 10,
            "accounts": [
                {
                    "id": "a1",
                    "name": "台北-小小",
                    "persona": '{"name": "小小", "gender": "女"}',
                    "is_running": True,
                    "groups": [-1002229799107],
                    "groups_available": [
                        {"id": -1002229799107, "title": "桃花源・約會"},
                        {"id": -1004567890123, "title": "另一個群"},
                    ],
                    "stats": {"replies_sent": 5, "proactive_sent": 2, "gate_held": 1},
                }
            ],
        }

    async def list_available_groups(self, account_id):
        return (
            [
                {"id": -1002229799107, "title": "桃花源・約會"},
                {"id": -1004567890123, "title": "另一個群"},
            ],
            "",
        )

    async def save_groups(self, account_id, group_ids):
        self._saved_groups[account_id] = list(group_ids)
        self.calls.append(("save_groups", account_id, tuple(group_ids)))
        return None

    def feature_status(self):
        return {
            "media_enabled": self._media,
            "media_generation_enabled": self._gen,
            "voice_enabled": self._voice,
            "voice_available": False,
        }

    async def update_feature_flags(
        self, media_enabled, voice_enabled, media_generation_enabled=None
    ):
        self.calls.append(
            ("features", media_enabled, voice_enabled, media_generation_enabled)
        )
        self._media = media_enabled
        self._voice = voice_enabled
        if media_generation_enabled is not None:
            self._gen = media_generation_enabled
        return None

    async def start(self, account_id):
        self.calls.append(("start", account_id))
        return None

    async def stop(self, account_id):
        self.calls.append(("stop", account_id))
        return None

    async def delete(self, account_id):
        self.calls.append(("delete", account_id))
        return None

    async def add_account(self, name, session_key, enable=False, display_name=""):
        self.calls.append(("add", name))
        return {"id": "a2", "name": name}


def _bot(monkeypatch, manager):
    return TgControlBot(_settings(monkeypatch), manager)


def test_make_bot_none_without_token(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "")
    assert make_bot(load_settings(), _FakeManager()) is None


def test_make_bot_returns_when_token_present(monkeypatch):
    assert isinstance(make_bot(_settings(monkeypatch), _FakeManager()), TgControlBot)


def test_admin_allowlist(monkeypatch):
    bot = _bot(monkeypatch, _FakeManager())

    class E:
        def __init__(self, uid):
            self.sender = type("S", (), {"id": uid})()

    assert bot._allowed(E(111)) is True
    assert bot._allowed(E(222)) is True
    assert bot._allowed(E(333)) is False


def test_status_text(monkeypatch):
    bot = _bot(monkeypatch, _FakeManager())
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(bot._status())
    finally:
        loop.close()
    assert "運行 3/3" in out
    assert "24h 送出 10｜攔截 6" in out
    assert "near_duplicate 4" in out
    assert "小小" in out
    assert "🟢" in out


def test_groups_list_and_messages(monkeypatch):
    bot = _bot(monkeypatch, _FakeManager())
    loop = asyncio.new_event_loop()
    try:
        listing = loop.run_until_complete(bot._groups([]))
        assert "桃花源" in listing
        assert "群組 -100" in listing or "-100" in listing
        msgs = loop.run_until_complete(bot._groups(["-100"]))
        assert "阿宏" in msgs
        assert "水軍·小小" in msgs
        assert "真人的「有人在嗎」" in msgs or "有人在嗎" in msgs
        # 預設 20 則上限內的參數解析
        capped = loop.run_until_complete(bot._groups(["-100", "999"]))
        assert "最近" in capped
    finally:
        loop.close()


def test_privates(monkeypatch):
    bot = _bot(monkeypatch, _FakeManager())
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(bot._privates(["台北-小小"]))
        assert "加我LINE" in out
        assert "🔵未讀" in out
        missing = loop.run_until_complete(bot._privates(["Nobody"]))
        assert "找不到帳號" in missing
        noarg = loop.run_until_complete(bot._privates([]))
        assert "用法" in noarg
    finally:
        loop.close()


def test_help():
    bot = TgControlBot.__new__(TgControlBot)
    # help 不依賴實例狀態，直接測純函式輸出即可
    h = TgControlBot._help(bot)
    assert "/status" in h
    assert "/groups" in h
    assert "/startacc" in h
    assert "/stopacc" in h
    assert "/deleteacc" in h
    assert "/media" in h
    assert "/addacc" in h


def test_acct_list(monkeypatch):
    bot = _bot(monkeypatch, _FakeManager())
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(bot._acct_list())
    finally:
        loop.close()
    assert "台北-小小" in out
    assert "🟢" in out  # worker 運行中


def test_acc_toggle_and_delete(monkeypatch):
    mgr = _FakeManager()
    bot = _bot(monkeypatch, mgr)
    loop = asyncio.new_event_loop()
    try:
        started = loop.run_until_complete(bot._acc_toggle("startacc", ["台北-小小"]))
        assert "已啟動" in started
        stopped = loop.run_until_complete(bot._acc_toggle("stopacc", ["台北-小小"]))
        assert "已停止" in stopped
        deleted = loop.run_until_complete(bot._acc_delete(["台北-小小"]))
        assert "已刪除" in deleted
        missing = loop.run_until_complete(bot._acc_toggle("startacc", [" nobody "]))
        assert "找不到帳號" in missing
    finally:
        loop.close()
    assert ("start", "a1") in mgr.calls
    assert ("stop", "a1") in mgr.calls
    assert ("delete", "a1") in mgr.calls


def test_feature_toggle(monkeypatch):
    mgr = _FakeManager()
    bot = _bot(monkeypatch, mgr)
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(bot._feature_toggle("media", ["off"]))
        # voice 維持現狀（False），media 被關，imagegen 維持現狀（True）
        assert ("features", False, False, True) in mgr.calls
        assert "媒體識別：關閉" in out
        # 不帶參數＝切換：媒體現在是 False → 切換後 True（imagegen 維持 True）
        out2 = loop.run_until_complete(bot._feature_toggle("media", []))
        assert ("features", True, False, True) in mgr.calls
        assert "媒體識別：開啟" in out2
        # 圖片生成獨立開關：on → off
        out3 = loop.run_until_complete(bot._feature_toggle("imagegen", ["off"]))
        assert ("features", True, False, False) in mgr.calls
        assert "圖片生成：關閉" in out3
    finally:
        loop.close()


def test_menu_labels_map_to_commands():
    from app.bot import MENU_LABELS, MENU_ROWS
    # 每顆按鈕都要能映射回指令（隱藏鍵是例外）
    assert MENU_LABELS["📊 狀態"] == "status"
    assert MENU_LABELS["👤 帳號"] == "acct"
    assert MENU_LABELS["🖼 媒體"] == "media"
    assert MENU_LABELS["⌨ 隱藏"] == ""
    for row in MENU_ROWS:
        assert len(row) <= 4
        for label, _ in row:
            assert label in MENU_LABELS


def test_dispatch_callback_uses_cb_edit(monkeypatch):
    """telethon 1.44 的 CallbackQuery 事件沒有 .message（是 get_message()），
    但事件本身有 edit()。callback 處理必須走 cb.edit 而非 cb.message.edit，
    否則炸 AttributeError: 'Event' object has no attribute 'message'。"""
    import unittest.mock as mock

    bot = _bot(monkeypatch, _FakeManager())
    loop = asyncio.new_event_loop()
    try:
        cb = mock.Mock()
        cb.data = "status"
        cb.edit = mock.AsyncMock(return_value=True)
        cb.answer = mock.AsyncMock(return_value=True)
        cb.message = mock.PropertyMock(
            side_effect=AttributeError("Event has no attribute 'message'")
        )
        loop.run_until_complete(bot._dispatch_callback(cb, "status"))
        cb.edit.assert_awaited_once()
    finally:
        loop.close()


def test_account_rows_with_int_groups(monkeypatch):
    """_account_rows 把群組 id（int）拼進文字；join 前若沒轉 str 會炸
    TypeError: sequence item 0: expected str instance, int found（主人按
    「👤 帳號」遇到的錯）。"""

    class _Mgr:
        class _DB:
            async def list_accounts(self):
                return [{"id": "a1", "name": "台北-小小"}]

        def __init__(self):
            self.db = self._DB()

        async def status(self):
            return {
                "accounts": [
                    {
                        "id": "a1",
                        "name": "台北-小小",
                        "is_running": True,
                        "groups": [-1002229799107, -1004567890123],
                    }
                ]
            }

    bot = TgControlBot.__new__(TgControlBot)
    bot.manager = _Mgr()
    loop = asyncio.new_event_loop()
    try:
        rows = loop.run_until_complete(bot._account_rows())
    finally:
        loop.close()
    assert len(rows) == 1
    assert "台北-小小" in rows[0]
    assert "-1002229799107" in rows[0]
    assert "-1004567890123" in rows[0]


def test_acc_label_prefers_tg_username(monkeypatch):
    """_acc_label 優先用 TG 顯示名（tg_username），沒有才回退內部帳號名。"""
    bot = _bot(monkeypatch, _FakeManager())
    # 有 TG 名 → 用 TG 名
    assert (
        bot._acc_label({"name": "水軍帳號 281", "tg_username": "小美（台北）"})
        == "小美（台北）"
    )
    # 無 TG 名 → 回退內部名
    assert (
        bot._acc_label({"name": "水軍帳號 281", "tg_username": None})
        == "水軍帳號 281"
    )
    # TG 名為空白字串 → 回退內部名
    assert (
        bot._acc_label({"name": "水軍帳號 281", "tg_username": "   "})
        == "水軍帳號 281"
    )


class _CbEvent:
    """fake CallbackQuery：帶 sender（權限檢查用）＋ edit/answer 記錄。"""

    def __init__(self, sender_id: int, data: str):
        self.data = data
        self.sender = type("S", (), {"id": sender_id})()
        self.edits: list[tuple] = []

    async def edit(self, *a, **k):
        self.edits.append((a, k))
        return True

    async def answer(self, *a, **k):
        return True


def _kb_data(kb) -> list[str]:
    """收集鍵盤所有按鈕的 callback data（bytes → str）。"""
    return [b.data.decode() for r in kb.rows for b in r.buttons]


def _kb_texts(kb) -> list[str]:
    """收集鍵盤所有按鈕的文字。"""
    return [b.text for r in kb.rows for b in r.buttons]


def test_callback_rejects_non_admin(monkeypatch):
    """BOT_ADMIN_IDS=111,222 時，333 按按鈕被擋＋彈提示；111 按則進入。"""
    import unittest.mock as mock

    bot = _bot(monkeypatch, _FakeManager())
    # 非管理者 333：用真實 telethon 的同步 sender_id 欄位
    cb = mock.Mock(spec=["sender_id", "data", "answer", "edit"])
    cb.data = "status"
    cb.sender_id = 333
    cb.answer = mock.AsyncMock(return_value=True)
    cb.edit = mock.AsyncMock(return_value=True)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(bot._on_callback(cb))
    finally:
        loop.close()
    cb.answer.assert_awaited_once()  # 彈「不是管理者」
    cb.edit.assert_not_awaited()  # 沒進入 dispatch
    # 管理者 111：進 dispatch（edit 被呼叫）
    cb2 = mock.Mock(spec=["sender_id", "data", "answer", "edit"])
    cb2.data = "status"
    cb2.sender_id = 111
    cb2.answer = mock.AsyncMock(return_value=True)
    cb2.edit = mock.AsyncMock(return_value=True)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(bot._on_callback(cb2))
    finally:
        loop.close()
    cb2.edit.assert_awaited_once()


def test_acc_delete_confirm_page(monkeypatch):
    bot = _bot(monkeypatch, _FakeManager())
    loop = asyncio.new_event_loop()
    try:
        body, kb = loop.run_until_complete(bot._acc_delete_confirm("a1"))
    finally:
        loop.close()
    assert "確認刪除" in body
    assert "台北-小小" in body
    data = _kb_data(kb)
    assert "acc.del.yes:a1" in data
    assert "acc.del.no:a1" in data


def test_groups_menu_and_save(monkeypatch):
    mgr = _FakeManager()
    bot = _bot(monkeypatch, mgr)
    loop = asyncio.new_event_loop()
    try:
        body, kb = loop.run_until_complete(bot._groups_menu("a1"))
        # 預選＝目前已設定的群組（只有 -1002229799107 已勾）
        assert "已選 1 個" in body
        data = _kb_data(kb)
        texts = _kb_texts(kb)
        assert any("桃花源・約會" in t for t in texts)  # 群組名在按鈕文字
        assert "g:-1002229799107:a1" in data
        assert "g:-1004567890123:a1" in data
        assert "g.save:a1" in data
        # 勾第二個群組
        loop.run_until_complete(
            bot._dispatch_callback(
                _CbEvent(111, "g:-1004567890123:a1"), "g:-1004567890123:a1"
            )
        )
        assert bot._group_selections["a1"] == {-1002229799107, -1004567890123}
        # 保存
        loop.run_until_complete(
            bot._dispatch_callback(_CbEvent(111, "g.save:a1"), "g.save:a1")
        )
        assert ("save_groups", "a1", (-1004567890123, -1002229799107)) in mgr.calls
        # 保存後暫存清掉
        assert "a1" not in bot._group_selections
    finally:
        loop.close()


def test_model_info(monkeypatch):
    bot = _bot(monkeypatch, _FakeManager())
    out = bot._model_info()
    assert "glm-5.3" in out
    assert "ollama.com/v1" in out
    assert "glm-5.3-flash" in out
    assert "kimi-k3" in out


def test_usage_info(monkeypatch):
    bot = _bot(monkeypatch, _FakeManager())
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(bot._usage_info())
    finally:
        loop.close()
    assert "17 次" in out
    assert "7000" in out
    assert "1100" in out
    assert "reply" in out
    assert "proactive" in out
    assert "門控暫緩" in out


def test_home_menu_has_model_and_usage(monkeypatch):
    from app.bot import TgControlBot

    bot = _bot(monkeypatch, _FakeManager())
    loop = asyncio.new_event_loop()
    try:
        kb = loop.run_until_complete(bot._home_keyboard())
    finally:
        loop.close()
    data = _kb_data(kb)
    assert "model" in data
    assert "usage" in data
    assert "media" in data


class _FakeLoginService:
    """Mini App 流程用的 fake login_service。"""

    def __init__(self):
        self.calls = []
        self.pending = {}

    async def start(self, phone):
        self.calls.append(("start", phone))
        aid = "auth-1"
        self.pending[aid] = type("P", (), {"auth_id": aid, "created_at": 0.0})()
        return {"phone_hint": phone[:4] + "****", "status": "code_required"}

    async def submit_code(self, auth_id, code):
        self.calls.append(("code", auth_id, code))
        return {"status": "password_required"}

    async def submit_password(self, auth_id, pw):
        self.calls.append(("pass", auth_id, pw))
        return {"status": "authorized"}

    async def claim(self, auth_id):
        self.calls.append(("claim", auth_id))
        return type(
            "V",
            (),
            {
                "session_string": "s:xyz",
                "tg_user_id": 42,
                "tg_name": "美玲",
                "avatar": "data:image/png;base64,AA",
            },
        )()


def test_mini_app_routes(monkeypatch):
    """Mini App：/tg/addacc 要令牌；/tgapi/start→code→pass→claim 串起來。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mgr = _FakeManager()
    ls = _FakeLoginService()
    bot = TgControlBot(_settings(monkeypatch), mgr, login_service=ls)
    host = FastAPI()
    host.include_router(bot.app)  # bot.app 是 APIRouter，要掛到 app 才能打
    client = TestClient(host)
    # 簽發一個令牌
    tok = secrets_token()
    bot._mini_tokens[tok] = time.time() + 3600

    # 無令牌 → 400
    r = client.post("/tgapi/start", json={"phone": "+886912345678"})
    assert r.status_code == 400
    # 有令牌 → 200
    r = client.post(
        f"/tgapi/start?t={tok}", json={"phone": "+886912345678"}
    )
    assert r.status_code == 200
    assert r.json().get("phone_hint")
    # code
    r = client.post(f"/tgapi/code?t={tok}", json={"code": "12345"})
    assert r.status_code == 200
    assert r.json().get("status") == "password_required"
    # pass
    r = client.post(f"/tgapi/pass?t={tok}", json={"password": "cloudpw"})
    assert r.status_code == 200
    assert r.json().get("status") == "authorized"
    # claim → 建立帳號
    r = client.post(f"/tgapi/claim?t={tok}", json={"name": "台北-美玲"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("tg_name") == "美玲"
    assert body["account"]["name"] == "台北-美玲"
    assert ("add", "台北-美玲") in mgr.calls
    # claim 成功後 token 應該被清
    assert tok not in bot._mini_tokens
    # login service 串接
    assert ("start", "+886912345678") in ls.calls
    assert ("code", "auth-1", "12345") in ls.calls
    assert ("pass", "auth-1", "cloudpw") in ls.calls


def test_mini_page_needs_token(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mgr = _FakeManager()
    bot = TgControlBot(_settings(monkeypatch), mgr, login_service=_FakeLoginService())
    host = FastAPI()
    host.include_router(bot.app)
    client = TestClient(host)
    # 無令牌 → 顯示過期
    r = client.get("/tg/addacc")
    assert "令牌過期" in r.text or "回 bot" in r.text
    # 有令牌 → 回完整 HTML
    tok = secrets_token()
    bot._mini_tokens[tok] = time.time() + 3600
    r = client.get(f"/tg/addacc?t={tok}")
    assert "新增水軍帳號" in r.text
    assert "doStart" in r.text


def test_mini_app_keyboard_url(monkeypatch):
    """Mini App 入口按鈕的 url 帶 token＋公網位址。"""
    bot = _bot(monkeypatch, _FakeManager())
    kb = bot._mini_app_keyboard("TOK123")
    btn = kb.rows[0].buttons[0]
    assert btn.url.endswith("/tg/addacc?t=TOK123")
    assert "localhost" in btn.url or "http://" in btn.url


def secrets_token():
    import secrets

    return secrets.token_urlsafe(16)
