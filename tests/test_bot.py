"""Telegram 控制台 bot 橋接的測試。"""

import asyncio
import time

import pytest

from app.bot import TgControlBot, make_bot
from app.config import load_settings


PHONE = "+886912345678"  # 測試用合成號碼（顯示層可能遮成 ****）


def _settings(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:FAKE-test-token-not-real")
    monkeypatch.setenv("BOT_ADMIN_IDS", "111, 222")
    return load_settings()


class _FakeDB:
    def __init__(self):
        self.accounts = {
            "a1": {
                "id": "a1",
                "name": "台北-小小",
                "persona": '{"name": "小小", "gender": "女"}',
                "enabled": 1,
                "reply_enabled": 1,
                "proactive_enabled": 1,
                "setup_complete": 1,
            }
        }
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
        self._c = self

    async def execute(self, sql, params=()):
        return self

    async def fetchall(self):
        return [
            (-100, "阿宏", "user", "有人在嗎", 1760000000.0),
            (-100, "小小", "assistant", "有啊哥哥", 1760000010.0),
        ]

    async def fetchone(self):
        return (1,)

    async def commit(self):
        return None

    async def get_account(self, account_id):
        return self.accounts.get(str(account_id))

    async def update_account(self, account_id, **k):
        acc = self.accounts.get(str(account_id))
        if acc is not None:
            acc.update(k)
        return None

    def messages(self):
        return [
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

    async def get_group_messages(self, gid, limit=100):
        assert gid == -100
        return list(self.messages())[:limit]

    async def group_overview(self, exclude_senders=()):
        return list(self.overview)

    async def get_group_labels(self):
        return dict(self.labels)

    async def list_accounts(self):
        return [{"id": "a1", "name": "台北-小小"}]

    async def get_private_messages(self, account_id, limit=50):
        assert account_id == "a1"
        return list(self.privates)[:limit]

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
        # 模擬真 db 的 SELECT *：帶 session_key 等內部欄位（claim 白名單要擋）
        return {
            "id": "a2",
            "name": name,
            "tg_username": display_name,
            "tg_name": display_name,
            "avatar": "",
            "status": "idle",
            "enabled": 0,
            "persona": "{}",
            "reply_enabled": 1,
            "proactive_enabled": 1,
            "created_at": 1760000000.0,
            "updated_at": 1760000000.0,
            "session_key": "SESSION-SECRET-xyz",
        }


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
        # #3：/deleteacc 回確認頁（一次性 token），按「✅ 確認刪除」才真刪
        body, kb = loop.run_until_complete(
            bot._acc_delete(["台北-小小"], _CbEvent(111, "x"))
        )
        assert "確認刪除" in body
        yes_data = [d for d in _kb_data(kb) if d.startswith("acc.del.yes:")]
        assert len(yes_data) == 1
        loop.run_until_complete(
            bot._dispatch_callback(_CbEvent(111, yes_data[0]), yes_data[0])
        )
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

    def __init__(self, sender_id: int, data: str, chat_id: int = 7):
        self.data = data
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.sender = type("S", (), {"id": sender_id})()
        self.edits: list[tuple] = []
        self.answers: list[tuple] = []

    async def edit(self, *a, **k):
        self.edits.append((a, k))
        return True

    async def answer(self, *a, **k):
        self.answers.append((a, k))
        return True


def _kb_data(kb) -> list[str]:
    """收集鍵盤所有按鈕的 callback data（bytes → str）。"""
    return [b.data.decode() for r in kb.rows for b in r.buttons]


def _kb_texts(kb) -> list[str]:
    """收集鍵盤所有按鈕的文字。"""
    return [b.text for r in kb.rows for b in r.buttons]


def _sign_initdata(bot, uid, chat_id=None, auth_date=None):
    """合成一個合法的 TG initData（HMAC 簽章＋auth_date），用測試 bot_token。

    只用合成密鑰（bot_token 已是 123456:FAKE...）與資料，不碰真實 Telegram。
    chat_id=None＝user JSON 不自帶 chat_id（TG 標準 initData 常見）。
    """
    import hashlib
    import hmac as _hmac
    import json as _json
    import time as _t
    import urllib.parse

    user = {"id": int(uid)}
    if chat_id is not None:
        user["chat_id"] = int(chat_id)
    user_json = _json.dumps(user, separators=(",", ":"))
    ad = str(int(auth_date if auth_date is not None else _t.time()))
    items = [("auth_date", ad), ("user", user_json)]
    check = "\n".join(f"{k}={v}" for k, v in sorted(items))
    secret = _hmac.new(b"WebAppData", bot.bot_token.encode(), hashlib.sha256).digest()
    h = _hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(dict(items, **{"hash": h}))


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
        body, kb = loop.run_until_complete(
            bot._acc_delete_confirm("a1", "111", 7)
        )
    finally:
        loop.close()
    assert "確認刪除" in body
    assert "台北-小小" in body
    data = _kb_data(kb)
    # yes／no 都綁同一份一次性 token（#3：取消不再按帳號亂找）
    assert any(d.startswith("acc.del.yes:") for d in data)
    yes_token = [d.split(":", 1)[1] for d in data if d.startswith("acc.del.yes:")][0]
    no_token = [d.split(":", 1)[1] for d in data if d.startswith("acc.del.no:")][0]
    assert yes_token == no_token  # 取消綁定同一份確認
    assert yes_token in bot._del_confirms
    acc_id, adm, chat, exp = bot._del_confirms[yes_token]
    assert (acc_id, adm, chat) == ("a1", "111", 7)


def test_groups_menu_and_save(monkeypatch):
    mgr = _FakeManager()
    bot = _bot(monkeypatch, mgr)
    loop = asyncio.new_event_loop()
    try:
        body, kb = loop.run_until_complete(bot._groups_menu("a1", "111", 7))
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
        # #9：選中狀態按 (管理員,聊天,帳號) 隔離
        assert bot._group_selections[("111", 7, "a1")]["sel"] == {
            -1002229799107, -1004567890123
        }
        # 保存
        loop.run_until_complete(
            bot._dispatch_callback(_CbEvent(111, "g.save:a1"), "g.save:a1")
        )
        assert ("save_groups", "a1", (-1004567890123, -1002229799107)) in mgr.calls
        # 保存後暫存清掉
        assert ("111", 7, "a1") not in bot._group_selections
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
    """faithful：回真正 TelegramLoginService 的 status 契約（無 need_password）。

    need_password=True → submit_code 回 password_required（兩步驗證）；
    False → submit_code 回 authorized（不需密碼）。auth_id 依呼叫序不同，
    防「都用 auth-1」掩蓋 A/B 隔離缺陷。
    """

    def __init__(self, need_password=False):
        self.calls = []
        self.pending = {}
        self.need_password = need_password
        self._n = 0

    def _hint(self, phone):
        return f"{phone[:4]}•••••{phone[-3:]}" if len(phone) > 7 else phone

    async def start(self, phone):
        self.calls.append(("start", phone))
        self._n += 1
        aid = f"auth-{self._n}"
        self.pending[aid] = type("P", (), {"auth_id": aid, "created_at": 0.0})()
        return {
            "phone_hint": self._hint(phone),
            "status": "code_required",
            "auth_id": aid,
            "expires_in": 300,
        }

    async def submit_code(self, auth_id, code):
        self.calls.append(("code", auth_id, code))
        if self.need_password:
            return {"status": "password_required", "phone_hint": "h",
                    "expires_in": 300}
        return {"status": "authorized", "phone_hint": "h", "expires_in": 300}

    async def submit_password(self, auth_id, pw):
        self.calls.append(("pass", auth_id, pw))
        return {"status": "authorized", "phone_hint": "h", "expires_in": 300}

    async def claim(self, auth_id):
        self.calls.append(("claim", auth_id))
        return type(
            "V", (), {
                "session_string": "s:" + str(auth_id),
                "tg_user_id": 42,
                "tg_name": "美玲",
                "avatar": "data:image/png;base64,AA",
            },
        )()

def test_mini_app_routes(monkeypatch):
    """Mini App：/tg/addacc 要令牌；/tgapi/start→code→claim（authorized 路徑）。

    身分＝initData（HMAC 簽章）；contract＝真正服務 status（無 need_password）。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mgr = _FakeManager()
    ls = _FakeLoginService(need_password=False)  # 不需兩步驗證 → authorized
    bot = TgControlBot(_settings(monkeypatch), mgr, login_service=ls)
    host = FastAPI()
    host.include_router(bot.app)
    client = TestClient(host)
    tok = bot._mini_mint("111", 7)
    init = _sign_initdata(bot, "111")

    # 無 token（但身分合法）→ 400
    r = client.post("/tgapi/start", json={"phone": PHONE, "init_data": init})
    assert r.status_code == 400
    # 有 token + 可信 initData → 200（bind auth_id + stage=code）
    r = client.post(
        f"/tgapi/start?t={tok}", json={"phone": PHONE, "init_data": init}
    )
    assert r.status_code == 200
    aid = r.json()["auth_id"]
    flow = bot._mini_flows[tok]
    assert flow.auth_id == aid and flow.stage == "code"
    # 錯階段：code 前直接 pass → 400
    r = client.post(
        f"/tgapi/pass?t={tok}", json={"password": "cloudpw", "init_data": init}
    )
    assert r.status_code == 400
    # code → authorized → stage=claim，need_password=false
    r = client.post(f"/tgapi/code?t={tok}", json={"code": "12345", "init_data": init})
    assert r.status_code == 200
    assert r.json().get("need_password") is False
    assert bot._mini_flows[tok].stage == "claim"
    # claim → 建立帳號（白名單）
    r = client.post(
        f"/tgapi/claim?t={tok}", json={"name": "台北-美玲", "init_data": init}
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("tg_name") == "美玲"
    assert body["account"]["name"] == "台北-美玲"
    assert "session_key" not in body["account"]
    assert ("add", "台北-美玲") in mgr.calls
    assert tok not in bot._mini_flows
    assert ("start", PHONE) in ls.calls
    assert ("code", aid, "12345") in ls.calls

def test_mini_flow_binding(monkeypatch):
    """#1/#2：initData 身分 + token 綁定 管理員/聊天/階段/期限；fail-closed。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mgr = _FakeManager()
    ls = _FakeLoginService()
    bot = TgControlBot(_settings(monkeypatch), mgr, login_service=ls)
    host = FastAPI()
    host.include_router(bot.app)
    client = TestClient(host)

    tok_a = bot._mini_mint("111", 7)
    tok_b = bot._mini_mint("222", 8)

    # 跨管理員：token A + 管理員 222 的 initData → 400
    r = client.post(
        f"/tgapi/start?t={tok_a}",
        json={"phone": PHONE, "init_data": _sign_initdata(bot, "222")},
    )
    assert r.status_code == 400
    # 跨聊天：token A + 111 但 initData 自帶 chat_id=999 → 400
    r = client.post(
        f"/tgapi/start?t={tok_a}",
        json={"phone": PHONE,
              "init_data": _sign_initdata(bot, "111", chat_id=999)},
    )
    assert r.status_code == 400
    # 偽造身分：無 initData、只自報 admin/chat → 400（fail-closed 不回落）
    r = client.post(
        f"/tgapi/start?t={tok_a}",
        json={"phone": PHONE, "admin_id": "111", "chat_id": 7},
    )
    assert r.status_code == 400
    # 正確身分（111，無 chat_id）→ 200
    r = client.post(
        f"/tgapi/start?t={tok_a}",
        json={"phone": PHONE, "init_data": _sign_initdata(bot, "111")},
    )
    assert r.status_code == 200
    # 並行：B 流程未 start 就 code → 400
    r = client.post(
        f"/tgapi/code?t={tok_b}",
        json={"code": "1", "init_data": _sign_initdata(bot, "222")},
    )
    assert r.status_code == 400
    # 過期：把 A flow 期限改過去 → 400
    tok_c = bot._mini_mint("111", 7)
    bot._mini_flows[tok_c].expires_at = time.time() - 1
    r = client.post(
        f"/tgapi/start?t={tok_c}",
        json={"phone": PHONE, "init_data": _sign_initdata(bot, "111")},
    )
    assert r.status_code == 400
    # 重放：A 已 claim 用完 token 再 claim → 400
    init111 = _sign_initdata(bot, "111")
    client.post(f"/tgapi/code?t={tok_a}", json={"code": "1", "init_data": init111})
    client.post(f"/tgapi/claim?t={tok_a}", json={"name": "X", "init_data": init111})
    r = client.post(
        f"/tgapi/claim?t={tok_a}", json={"name": "X2", "init_data": init111}
    )
    assert r.status_code == 400

def test_mini_initdata_verify(monkeypatch):
    """#2：initData HMAC 驗簽——不能只相信客戶端 user id。"""
    import hashlib
    import hmac as _hmac
    import json as _json
    import urllib.parse

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mgr = _FakeManager()
    ls = _FakeLoginService()
    bot = TgControlBot(_settings(monkeypatch), mgr, login_service=ls)
    host = FastAPI()
    host.include_router(bot.app)
    client = TestClient(host)
    tok = bot._mini_mint("111", 7)

    def sign(uid: str) -> str:
        user = _json.dumps({"id": int(uid)}, separators=(",", ":"))
        items = [("user", user)]
        check = "\n".join(f"{k}={v}" for k, v in sorted(items))
        secret = _hmac.new(b"WebAppData", bot.bot_token.encode(), hashlib.sha256).digest()
        h = _hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urllib.parse.urlencode(dict(items, **{"hash": h}))

    # 正確簽章的 initData（管理員 111）→ 200
    r = client.post(
        f"/tgapi/start?t={tok}", json={"phone": "+886****5678", "init_data": sign("111")}
    )
    assert r.status_code == 200
    # 錯簽章（改 hash）→ 400
    bad = sign("111").replace("hash=", "hash=deadbeef")
    tok2 = bot._mini_mint("111", 7)
    r = client.post(
        f"/tgapi/start?t={tok2}",
        json={"phone": "+886****5678", "init_data": bad},
    )
    assert r.status_code == 400
    # 簽章對但管理員不是簽發者 → 400
    tok3 = bot._mini_mint("111", 7)
    r = client.post(
        f"/tgapi/start?t={tok3}", json={"phone": "+886****5678", "init_data": sign("333")}
    )
    assert r.status_code == 400


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
    tok = bot._mini_mint("111", 7)
    r = client.get(f"/tg/addacc?t={tok}")
    assert "新增水軍帳號" in r.text
    assert "doStart" in r.text


def test_mini_app_keyboard_url(monkeypatch):
    """Mini App 入口按鈕的 url 帶 token＋公網位址。"""
    bot = _bot(monkeypatch, _FakeManager())
    kb = bot._mini_keyboard_token("TOK123", "111", 7)
    btn = kb.rows[0].buttons[0]
    assert "/tg/addacc?t=TOK123" in btn.url
    # #2：admin/chat 綁在 URL（initData 缺失時的備援身分）
    assert "admin=111" in btn.url and "chat=7" in btn.url
    assert "localhost" in btn.url or "http://" in btn.url


def secrets_token():
    import secrets

    return secrets.token_urlsafe(16)


# ======================================================================
# 663ec51 獨立驗收：失敗路徑 / 重複操作 / 整合
# ======================================================================


def test_admin_allowlist_fail_closed(monkeypatch):
    """#1：BOT_ADMIN_IDS 為空＝預設拒絕（不能靠環境變數已設定就放行）。"""

    class E:
        def __init__(self, uid):
            self.sender_id = uid
            self.sender = type("S", (), {"id": uid})()

    class M:
        def __init__(self):
            self.db = _FakeDB()
            self.workers = {}

    from app.config import load_settings
    from app.bot import TgControlBot

    monkeypatch.setenv("BOT_TOKEN", "1:FAKE")
    monkeypatch.setenv("BOT_ADMIN_IDS", "")
    bot = TgControlBot(load_settings(), M())
    assert bot._allowed(E(111)) is False
    monkeypatch.setenv("BOT_ADMIN_IDS", "111")
    bot2 = TgControlBot(load_settings(), M())
    assert bot2._allowed(E(111)) is True
    assert bot2._allowed(E(222)) is False

    class ENoId:
        sender_id = None
        sender = None

    assert bot2._allowed(ENoId()) is False



def test_delete_confirm_reuse_and_cancel(monkeypatch):
    """#3：重複點「✅ 確認刪除」不得二次刪除；過期/錯管理員/取消都要失效。"""
    import time as _t

    mgr = _FakeManager()
    bot = _bot(monkeypatch, mgr)
    loop = asyncio.new_event_loop()
    try:
        body, kb = loop.run_until_complete(bot._acc_delete_confirm("a1", "111", 7))
        yes = [d for d in _kb_data(kb) if d.startswith("acc.del.yes:")][0]
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(111, yes), yes))
        assert mgr.calls.count(("delete", "a1")) == 1
        ev2 = _CbEvent(111, yes)
        loop.run_until_complete(bot._dispatch_callback(ev2, yes))
        assert mgr.calls.count(("delete", "a1")) == 1
        assert len(ev2.answers) == 1  # 彈「確認已過期」提示，不再刪
        _, kb = loop.run_until_complete(bot._acc_delete_confirm("a1", "111", 7))
        yes2 = [d for d in _kb_data(kb) if d.startswith("acc.del.yes:")][0]
        bot._del_confirms[yes2.split(":", 1)[1]] = ("a1", "111", 7, _t.time() - 1)
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(111, yes2), yes2))
        assert mgr.calls.count(("delete", "a1")) == 1
        _, kb = loop.run_until_complete(bot._acc_delete_confirm("a1", "111", 7))
        yes3 = [d for d in _kb_data(kb) if d.startswith("acc.del.yes:")][0]
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(222, yes3), yes3))
        assert mgr.calls.count(("delete", "a1")) == 1
        _, kb = loop.run_until_complete(bot._acc_delete_confirm("a1", "111", 7))
        data = _kb_data(kb)
        yes4 = [d for d in data if d.startswith("acc.del.yes:")][0]
        no4 = [d for d in data if d.startswith("acc.del.no:")][0]
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(111, no4), no4))
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(111, yes4), yes4))
        assert mgr.calls.count(("delete", "a1")) == 1
    finally:
        loop.close()



def test_groups_paging_search_isolation(monkeypatch):
    """#9：搜尋、空結果、按 管理員/聊天/帳號 隔離。"""
    mgr = _FakeManager()
    bot = _bot(monkeypatch, mgr)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(bot._groups_menu("a1", "111", 7))
        loop.run_until_complete(bot._groups_menu("a1", "222", 8))
        loop.run_until_complete(
            bot._dispatch_callback(
                _CbEvent(111, "g:-1004567890123:a1"), "g:-1004567890123:a1"
            )
        )
        assert bot._group_selections[("111", 7, "a1")]["sel"] == {
            -1002229799107, -1004567890123
        }
        assert bot._group_selections[("222", 8, "a1")]["sel"] == {-1002229799107}
        body, _kb = loop.run_until_complete(
            bot._gsearch(["a1", "不存在的字串"], _CbEvent(111, "x"))
        )
        assert "沒有符合" in body
        body, kb = loop.run_until_complete(
            bot._gsearch(["a1", "桃花源"], _CbEvent(111, "x"))
        )
        assert "第 1/1 頁（共 1 個）" in body
        assert any("桃花源" in t for t in _kb_texts(kb))
        body, kb = loop.run_until_complete(
            bot._gsearch(["a1"], _CbEvent(111, "x"))
        )
        assert "第 1/1 頁（共 2 個）" in body
        assert any("桃花源" in t for t in _kb_texts(kb))
    finally:
        loop.close()



def test_account_flag_toggle_updates_worker(monkeypatch):
    """#10：每帳號 回覆/主動 開關＝db＋運行中 worker 即時更新。"""
    mgr = _FakeManager()
    bot = _bot(monkeypatch, mgr)
    worker = mgr.workers["a1"]
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(bot._acc_flag_toggle("a1", "reply_enabled"))
        assert "回覆開關：關" in out
        assert worker.reply_enabled is False
        assert mgr.db.accounts["a1"]["reply_enabled"] == 0
        out = loop.run_until_complete(bot._acc_flag_toggle("a1", "proactive_enabled"))
        assert "主動開關：關" in out
        assert worker.proactive_enabled is False
    finally:
        loop.close()



def test_persona_save_whitelist(monkeypatch):
    """#10：人設編輯白名單——密鑰欄位進不來。"""
    import json as _json

    mgr = _FakeManager()
    bot = _bot(monkeypatch, mgr)
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(
            bot._persona_save(
                "a1", '{"name": "小美", "api_key": "SECRET-KEY-1", "job": "工程師"}'
            )
        )
        assert "已更新" in out
        assert "api_key" in out
        persona = _json.loads(mgr.db.accounts["a1"]["persona"])
        assert persona["name"] == "小美"
        assert persona["job"] == "工程師"
        assert "api_key" not in persona
        out = loop.run_until_complete(bot._persona_save("a1", '{"foo": 1}'))
        assert "沒有有效欄位" in out
        out = loop.run_until_complete(bot._persona_save("a1", "不是JSON"))
        assert "不是 JSON" in out
    finally:
        loop.close()



def test_acc_logs(monkeypatch):
    """#10：日誌頁回最近收發。"""
    bot = _bot(monkeypatch, _FakeManager())
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(bot._acc_logs("a1"))
    finally:
        loop.close()
    assert "最近 2 則" in out
    assert "有人在嗎" in out
    assert "有啊哥哥" in out



def test_bot_run_retry_and_state(monkeypatch):
    """#11：run() 斷線自動重連；state() 回連上/斷線/最後錯誤。"""
    from app.config import load_settings
    from app.bot import TgControlBot

    class _Mgr:
        def __init__(self):
            self.db = _FakeDB()
            self.workers = {}

    monkeypatch.setenv("BOT_TOKEN", "1:FAKE")
    monkeypatch.setenv("BOT_ADMIN_IDS", "111")
    bot = TgControlBot(load_settings(), _Mgr())
    calls = {"n": 0}

    async def fake_run_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("第一次斷線")
        bot._stop_event.set()

    monkeypatch.setattr(bot, "_run_once", fake_run_once)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.wait_for(bot.run(), timeout=20))
    finally:
        loop.close()
    assert calls["n"] == 2
    bot2 = TgControlBot(load_settings(), _Mgr())
    st = bot2.state()
    assert st["connected"] is False
    assert st["last_error"] == ""
    assert st["username"] == ""



def test_health_includes_bot(monkeypatch):
    """#11：/health 含 bot 狀態（防 health 3/3 但 bot 死）。"""
    from app.config import load_settings
    from app.dashboard import Dashboard
    from app.bot import TgControlBot

    monkeypatch.setenv("BOT_TOKEN", "1:FAKE")
    monkeypatch.setenv("BOT_ADMIN_IDS", "111")
    settings = load_settings()
    d = Dashboard(settings, _FakeManager(), None)
    bot = TgControlBot(settings, _FakeManager())
    d.attach_bot(bot)
    loop = asyncio.new_event_loop()
    try:
        payload = loop.run_until_complete(d._health())
    finally:
        loop.close()
    assert "bot" in payload
    assert payload["bot"]["connected"] is False
    assert "last_error" in payload["bot"]
    d2 = Dashboard(settings, _FakeManager(), None)
    loop = asyncio.new_event_loop()
    try:
        payload2 = loop.run_until_complete(d2._health())
    finally:
        loop.close()
    assert "bot" not in payload2



def test_mini_routes_with_static_mount(monkeypatch):
    """路由要與 Dashboard 的根 StaticFiles mount 整合（663ec51 的 404 根因）。"""
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from fastapi.testclient import TestClient

    host = FastAPI()
    host.mount("/", StaticFiles(directory="app/static", html=True), name="static")
    mgr = _FakeManager()
    ls = _FakeLoginService()
    bot = TgControlBot(_settings(monkeypatch), mgr, login_service=ls)
    routes = host.router.routes
    n = len(routes)
    host.include_router(bot.app)
    assert len(routes) > n
    inc = routes.pop()
    routes.insert(0, inc)

    client = TestClient(host)
    tok = bot._mini_mint("111", 7)
    init = _sign_initdata(bot, "111")
    # GET /tg/addacc?t= → 200（以前被 static 吃掉回 404）
    r = client.get("/tg/addacc?t=" + tok)
    assert r.status_code == 200
    assert "新增水軍帳號" in r.text
    # POST /tgapi/start（可信 initData）→ 200
    r = client.post(
        "/tgapi/start?t=" + tok, json={"phone": PHONE, "init_data": init}
    )
    assert r.status_code == 200
    assert r.json().get("auth_id")
    # 無 token 的 POST（身分合法）→ 400（route 存在，不是 405/404）
    r = client.post("/tgapi/start", json={"phone": PHONE, "init_data": init})
    assert r.status_code == 400
    # static 根路徑仍正常（/openapi.json 不是 bot 路由）
    r2 = client.get("/openapi.json")
    assert r2.status_code == 200


# ======================================================================
# 2083952 獨立驗收：3 項必修 + 2 疑點的失敗路徑矩陣
# ======================================================================


def test_mini_identity_matrix(monkeypatch):
    """#1：四個敏感階段的身分缺失／偽造／無效／過期／錯綁定／重放 都要拒絕，
    且被拒時對應 login_service 不被呼叫。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mgr = _FakeManager()
    ls = _FakeLoginService()
    bot = TgControlBot(_settings(monkeypatch), mgr, login_service=ls)
    host = FastAPI()
    host.include_router(bot.app)
    client = TestClient(host)
    init111 = _sign_initdata(bot, "111")

    # --- start：無 initData → 400，且 start 不被呼叫 ---
    t1 = bot._mini_mint("111", 7)
    r = client.post(f"/tgapi/start?t={t1}", json={"phone": PHONE})
    assert r.status_code == 400
    assert not any(c[0] == "start" for c in ls.calls)
    # --- start：initData 空字串 → 400 ---
    r = client.post(f"/tgapi/start?t={t1}", json={"phone": PHONE, "init_data": ""})
    assert r.status_code == 400
    # --- start：無效簽章（hash 被改）→ 400 ---
    bad = init111.replace("hash=", "hash=" + "f" * 8)
    r = client.post(f"/tgapi/start?t={t1}", json={"phone": PHONE, "init_data": bad})
    assert r.status_code == 400
    # --- start：auth_date 過舊（>600s）→ 400 ---
    import time as _t
    stale = _sign_initdata(bot, "111", auth_date=_t.time() - 700)
    r = client.post(f"/tgapi/start?t={t1}", json={"phone": PHONE, "init_data": stale})
    assert r.status_code == 400
    # --- start：錯管理員（token 簽 111，initData 簽 222）→ 400 ---
    r = client.post(
        f"/tgapi/start?t={t1}",
        json={"phone": PHONE, "init_data": _sign_initdata(bot, "222")},
    )
    assert r.status_code == 400
    # --- start：偽造 admin/chat（無 initData）→ 400 ---
    r = client.post(
        f"/tgapi/start?t={t1}",
        json={"phone": PHONE, "admin_id": "111", "chat_id": 7},
    )
    assert r.status_code == 400
    # --- 合法 start 成功，建一個 code 流程 ---
    ok = client.post(
        f"/tgapi/start?t={t1}", json={"phone": PHONE, "init_data": init111}
    )
    assert ok.status_code == 200
    aid = ok.json()["auth_id"]
    # --- code：無 initData → 400（且 submit_code 不被呼叫）---
    r = client.post(f"/tgapi/code?t={t1}", json={"code": "12345"})
    assert r.status_code == 400
    assert not any(c[0] == "code" for c in ls.calls)
    # --- code：錯管理員 initData → 400 ---
    r = client.post(
        f"/tgapi/code?t={t1}",
        json={"code": "12345", "init_data": _sign_initdata(bot, "222")},
    )
    assert r.status_code == 400
    # --- 錯 token（不存在）→ 400 ---
    r = client.post(
        "/tgapi/code?t=NOPE", json={"code": "12345", "init_data": init111}
    )
    assert r.status_code == 400


def test_login_contract_password_required(monkeypatch):
    """#2：需兩步驗證的完整 start→code→pass→claim 閉環（status 契約）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mgr = _FakeManager()
    ls = _FakeLoginService(need_password=True)
    bot = TgControlBot(_settings(monkeypatch), mgr, login_service=ls)
    host = FastAPI()
    host.include_router(bot.app)
    client = TestClient(host)
    tok = bot._mini_mint("111", 7)
    init = _sign_initdata(bot, "111")

    r = client.post(f"/tgapi/start?t={tok}", json={"phone": PHONE, "init_data": init})
    assert r.status_code == 200
    aid = r.json()["auth_id"]
    # code → password_required → stage=pass，need_password=true
    r = client.post(f"/tgapi/code?t={tok}", json={"code": "12345", "init_data": init})
    assert r.status_code == 200
    assert r.json().get("need_password") is True
    assert bot._mini_flows[tok].stage == "pass"
    # pass → authorized → stage=claim
    r = client.post(f"/tgapi/pass?t={tok}", json={"password": "p", "init_data": init})
    assert r.status_code == 200
    assert r.json().get("status") == "authorized"
    assert bot._mini_flows[tok].stage == "claim"
    # claim 成功
    r = client.post(f"/tgapi/claim?t={tok}", json={"name": "N", "init_data": init})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "session_key" not in r.json()["account"]
    # 串接
    assert ("start", PHONE) in ls.calls
    assert ("code", aid, "12345") in ls.calls
    assert ("pass", aid, "p") in ls.calls
    assert ("claim", aid) in ls.calls


def test_login_contract_unknown_status(monkeypatch):
    """#2：未知 status → 明確拒絕（不冒稱成功、stage 不變）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class WeirdLS(_FakeLoginService):
        async def submit_code(self, auth_id, code):
            self.calls.append(("code", auth_id, code))
            return {"status": "weird_state", "phone_hint": "h", "expires_in": 1}

    mgr = _FakeManager()
    ls = WeirdLS()
    bot = TgControlBot(_settings(monkeypatch), mgr, login_service=ls)
    host = FastAPI()
    host.include_router(bot.app)
    client = TestClient(host)
    tok = bot._mini_mint("111", 7)
    init = _sign_initdata(bot, "111")
    client.post(f"/tgapi/start?t={tok}", json={"phone": PHONE, "init_data": init})
    r = client.post(f"/tgapi/code?t={tok}", json={"code": "1", "init_data": init})
    assert r.status_code == 400
    assert bot._mini_flows[tok].stage == "code"  # 沒被推進


def test_login_contract_dual_authid_parallel(monkeypatch):
    """#2：兩個不同 auth_id 並行，A/B 流程互不串（不能用都叫 auth-1 掩蓋）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mgr = _FakeManager()
    ls = _FakeLoginService(need_password=True)
    bot = TgControlBot(_settings(monkeypatch), mgr, login_service=ls)
    host = FastAPI()
    host.include_router(bot.app)
    client = TestClient(host)
    tok_a = bot._mini_mint("111", 7)
    tok_b = bot._mini_mint("222", 8)
    init_a = _sign_initdata(bot, "111")
    init_b = _sign_initdata(bot, "222")

    ra = client.post(f"/tgapi/start?t={tok_a}", json={"phone": PHONE, "init_data": init_a})
    rb = client.post(f"/tgapi/start?t={tok_b}", json={"phone": PHONE, "init_data": init_b})
    aid_a = ra.json()["auth_id"]
    aid_b = rb.json()["auth_id"]
    assert aid_a != aid_b  # 兩個真的不同 auth_id
    # A code、B code 各自推進，互不影響
    ra2 = client.post(f"/tgapi/code?t={tok_a}", json={"code": "1", "init_data": init_a})
    rb2 = client.post(f"/tgapi/code?t={tok_b}", json={"code": "2", "init_data": init_b})
    assert ra2.status_code == 200 and rb2.status_code == 200
    assert bot._mini_flows[tok_a].stage == "pass"
    assert bot._mini_flows[tok_b].stage == "pass"
    # A pass → A claim；B 仍停 pass（不能拿 B token 走 A 的 claim）
    client.post(f"/tgapi/pass?t={tok_a}", json={"password": "p", "init_data": init_a})
    r = client.post(f"/tgapi/claim?t={tok_b}", json={"name": "X", "init_data": init_b})
    assert r.status_code == 400  # B 還在 pass 階段，不能 claim
    assert bot._mini_flows[tok_a].stage == "claim"
    assert bot._mini_flows[tok_b].stage == "pass"


def test_delete_confirm_matrix(monkeypatch):
    """#3：刪除確認 正常/重複/錯chat/錯admin/過期/雙confirm取消指定。"""
    import time as _t

    mgr = _FakeManager()
    bot = _bot(monkeypatch, mgr)
    loop = asyncio.new_event_loop()

    def _yes(kb):
        return [d for d in _kb_data(kb) if d.startswith("acc.del.yes:")][0]

    def _no(kb):
        return [d for d in _kb_data(kb) if d.startswith("acc.del.no:")][0]

    try:
        # 1) 正常確認一次成功
        _, kb = loop.run_until_complete(bot._acc_delete_confirm("a1", "111", 7))
        yes = _yes(kb)
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(111, yes, chat_id=7), yes))
        assert mgr.calls.count(("delete", "a1")) == 1
        # 2) 重複點同一 token → 不再刪
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(111, yes, chat_id=7), yes))
        assert mgr.calls.count(("delete", "a1")) == 1
        # 3) 錯聊天室：chat 7 簽的，chat 999 按 → 不刪
        _, kb = loop.run_until_complete(bot._acc_delete_confirm("a1", "111", 7))
        yes = _yes(kb)
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(111, yes, chat_id=999), yes))
        assert mgr.calls.count(("delete", "a1")) == 1
        # 4) 錯管理員：111 簽的，222 按 → 不刪
        _, kb = loop.run_until_complete(bot._acc_delete_confirm("a1", "111", 7))
        yes = _yes(kb)
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(222, yes, chat_id=7), yes))
        assert mgr.calls.count(("delete", "a1")) == 1
        # 5) 過期：期限改過去 → 不刪
        _, kb = loop.run_until_complete(bot._acc_delete_confirm("a1", "111", 7))
        yes = _yes(kb)
        bot._del_confirms[yes.split(":", 1)[1]] = ("a1", "111", 7, _t.time() - 1)
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(111, yes, chat_id=7), yes))
        assert mgr.calls.count(("delete", "a1")) == 1
        # 6) 雙 confirm：C1、C2 同帳號；取消 C2 → C2 yes 必拒；C1 仍有效
        _, kb1 = loop.run_until_complete(bot._acc_delete_confirm("a1", "111", 7))
        yes1 = _yes(kb1)
        _, kb2 = loop.run_until_complete(bot._acc_delete_confirm("a1", "111", 7))
        yes2 = _yes(kb2)
        no2 = _no(kb2)
        assert yes2.split(":", 1)[1] != yes1.split(":", 1)[1]  # 不同 token
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(111, no2, chat_id=7), no2))
        # 取消 C2 後點 C2 yes → 拒（不刪）
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(111, yes2, chat_id=7), yes2))
        assert mgr.calls.count(("delete", "a1")) == 1
        # C1 未取消 → 點 C1 yes 仍刪（策略：取消只失效該份）
        loop.run_until_complete(bot._dispatch_callback(_CbEvent(111, yes1, chat_id=7), yes1))
        assert mgr.calls.count(("delete", "a1")) == 2
    finally:
        loop.close()


def test_persona_save_uses_manager_protection(monkeypatch):
    """#5：人設保存複用 manager.update_persona 的 fixed-persona 保護。"""
    mgr = _FakeManager()
    bot = _bot(monkeypatch, mgr)
    loop = asyncio.new_event_loop()
    try:
        # 一般帳號：update_persona 回 dict → 成功
        async def up_ok(account_id, persona):
            mgr.db.accounts[account_id]["persona"] = __import__("json").dumps(persona, ensure_ascii=False)
            return persona

        mgr.update_persona = up_ok
        out = loop.run_until_complete(bot._persona_save("a1", '{"name": "小美"}'))
        assert "已更新" in out
        # 受保護：update_persona 回 None → 拒絕（不冒稱成功）
        async def up_blocked(account_id, persona):
            return None

        mgr.update_persona = up_blocked
        out = loop.run_until_complete(bot._persona_save("a1", '{"name": "小美"}'))
        assert "受保護" in out
    finally:
        loop.close()


def test_model_info_readonly(monkeypatch):
    """#4：模型頁只讀顯示，不聲稱可編輯。"""
    bot = _bot(monkeypatch, _FakeManager())
    out = bot._model_info()
    assert "只讀顯示" in out
    assert "尚未完成" in out
    assert "glm-5.3" in out
