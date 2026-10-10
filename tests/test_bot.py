"""Telegram 控制台 bot 橋接的測試。"""

import asyncio

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


class _FakeManager:
    def __init__(self):
        self.db = _FakeDB()
        self.workers = {
            "a1": type("W", (), {"is_running": True})(),
        }
        self.calls = []
        self._media = True
        self._voice = False

    async def status(self):
        return {
            "running": 3,
            "total": 3,
            "reply_audit": {
                "sent": {"ok": 10},
                "policy": {"near_duplicate": 4, "gate_held": 2},
            },
            "accounts": [
                {
                    "id": "a1",
                    "name": "台北-小小",
                    "persona": '{"name": "小小", "gender": "女"}',
                    "is_running": True,
                    "stats": {"replies_sent": 5, "proactive_sent": 2, "gate_held": 1},
                }
            ],
        }

    def feature_status(self):
        return {
            "media_enabled": self._media,
            "voice_enabled": self._voice,
            "voice_available": False,
        }

    async def update_feature_flags(self, media_enabled, voice_enabled):
        self.calls.append(("features", media_enabled, voice_enabled))
        self._media = media_enabled
        self._voice = voice_enabled
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
        # voice 維持現狀（False），media 被關
        assert ("features", False, False) in mgr.calls
        assert "媒體：關閉" in out
    finally:
        loop.close()
