import asyncio
import os

from app.config import load_settings
from app.crypto import SecretBox
from app.database import Database
from app.manager import AccountManager
from app.worker import AccountWorker

DB = "/tmp/sdf_test/worker_groups_test.db"


def _config():
    s = load_settings()
    s.ai_model = ""  # 讓 _call_ai 直接回 ""，不真的呼叫 AI
    return s


class _FakeUser:
    first_name = "小王"
    last_name = None
    username = "xiaowang"


class _FakeEvent:
    def __init__(self, chat_id, sender_id=12345):
        self.is_private = False
        self.is_group = True
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.raw_text = "你好"
        self.mentioned = False
        self.is_reply = False
        self.reply_to = None

    async def get_sender(self):
        return _FakeUser()


def _make_worker(db, account_id, selected_groups):
    cfg = _config()
    w = AccountWorker(
        account_id=account_id, session_key="k", tg_api_id=1, tg_api_hash="h",
        ai_client=None, db=db, config=cfg, managed_ids=set(),
        on_status_change=lambda *a, **k: None, selected_groups=selected_groups,
    )
    # 模擬已連線（on_message 需要 is_running + tg_client 才處理）
    w.is_running = True
    w.tg_client = object()
    return w


def test_worker_selected_groups_gates_messages():
    """指定群組：非勾選群的消息被忽略（不記錄、不回覆）；勾選群才處理"""
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        worker = _make_worker(db, "w1", [-1001])
        # 非指定群（-1002）→ 應被忽略
        await worker.on_message(_FakeEvent(-1002))
        assert len(await db.get_recent_messages("w1", -1002)) == 0
        # 指定群（-1001）→ 應記錄
        await worker.on_message(_FakeEvent(-1001))
        assert len(await db.get_recent_messages("w1", -1001)) >= 1
        await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


def test_worker_no_selected_groups_denies_all():
    """沒指定群組（空）必須 fail-closed，不處理任何群消息。"""
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        worker = _make_worker(db, "w2", [])
        await worker.on_message(_FakeEvent(-9999))
        assert len(await db.get_recent_messages("w2", -9999)) == 0
        await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


def test_group_list_reflects_dialogs():
    """group_list() 回傳 _dialogs 裡的群（供控制台勾選）"""
    cfg = _config()
    worker = AccountWorker(
        account_id="w3", session_key="k", tg_api_id=1, tg_api_hash="h",
        ai_client=None, db=None, config=cfg, managed_ids=set(),
        on_status_change=lambda *a, **k: None, selected_groups=[],
    )
    worker._dialogs = {-1001: "台北約會群", -1002: "台中水群"}
    gl = worker.group_list()
    assert {g["id"] for g in gl} == {-1001, -1002}
    assert any(g["title"] == "台北約會群" for g in gl)


def test_manager_update_persona_and_save_groups():
    """manager.update_persona / save_groups：更新 DB + 熱更新運行中 worker"""
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        cfg = _config()
        box = SecretBox(cfg.account_encryption_key)
        manager = AccountManager(cfg, db, box)
        # 造一個帳號（persona 先存個預設）
        await db.create_account("m1", "測試", "x",
                                '{"name":"預設","personality":"預設"}')
        # 造假 worker（只驗熱更新，不真的連 Telegram）
        class FakeWorker:
            is_running = True
            persona = {"name": "預設"}
            name = "預設"
            selected_groups = set()

            def __init__(self):
                self.stopped = False

            async def stop(self):
                self.stopped = True
                self.is_running = False
        fw = FakeWorker()
        manager.workers["m1"] = fw

        # 改人設（含性格）
        new_p = {"name": "阿美", "gender": "女", "personality": "性感辣妹、愛撩人",
                 "city": "高雄", "hobbies": ["看電影", "吃美食"]}
        saved = await manager.update_persona("m1", new_p)
        assert saved["personality"] == "性感辣妹、愛撩人"
        acc = await db.get_account("m1")
        import json
        assert json.loads(acc["persona"])["personality"] == "性感辣妹、愛撩人"
        assert fw.persona["name"] == "阿美"  # 熱更新
        assert fw.name == "阿美"

        # 指定群組
        assert await manager.save_groups("m1", [-1002, -1001]) == ""
        acc2 = await db.get_account("m1")
        assert json.loads(acc2["groups"]) == [-1002, -1001]  # 內部排序
        assert fw.selected_groups == {-1001, -1002}  # 熱更新

        # 清空白名單必須立即停用，不能退回「所有群組」。
        assert await manager.save_groups("m1", []) == ""
        acc3 = await db.get_account("m1")
        assert json.loads(acc3["groups"]) == []
        assert acc3["setup_complete"] == 0
        assert acc3["enabled"] == 0
        assert fw.selected_groups == set()
        assert fw.stopped is True
        assert "m1" not in manager.workers

        # 不存在的帳號
        assert await manager.save_groups("nope", [1]) == "帳號不存在"
        await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


def test_manager_discovers_groups_without_starting_worker(monkeypatch):
    """群組清單使用短暫唯讀 Telegram 連線，完成後斷線且不建立互動 worker。"""
    if os.path.exists(DB):
        os.remove(DB)

    clients = []

    class FakeDialog:
        def __init__(self, dialog_id, title, is_group=True):
            self.id = dialog_id
            self.title = title
            self.is_group = is_group

    class FakeClient:
        def __init__(self, session, api_id, api_hash):
            self.connected = False
            self.disconnected = False
            clients.append(self)

        async def connect(self):
            self.connected = True

        async def get_me(self):
            return object()

        async def disconnect(self):
            self.disconnected = True

        async def iter_dialogs(self):
            yield FakeDialog(-1001, "台北交友群")
            yield FakeDialog(-1002, "台中聊天群")
            yield FakeDialog(99, "公告頻道", is_group=False)

    monkeypatch.setattr("app.manager.StringSession", lambda value: value, raising=False)
    monkeypatch.setattr("app.manager.TelegramClient", FakeClient, raising=False)

    async def main():
        db = Database(DB)
        await db.connect()
        cfg = _config()
        box = SecretBox(cfg.account_encryption_key)
        manager = AccountManager(cfg, db, box)
        await db.create_account("m2", "停止中的帳號", box.encrypt("session"))

        groups, err = await manager.list_available_groups("m2")

        assert err == ""
        assert groups == [
            {"id": -1002, "title": "台中聊天群"},
            {"id": -1001, "title": "台北交友群"},
        ]
        assert manager.workers == {}
        assert clients and clients[0].connected is True
        assert clients[0].disconnected is True

        await manager.aclose()
        await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


def test_manager_requires_group_setup_before_start(monkeypatch):
    """新帳號未確認群組範圍前不得啟動，避免登入後直接在所有群互動。"""
    if os.path.exists(DB):
        os.remove(DB)

    start_calls = []

    async def fake_start(self, account):
        start_calls.append(account["id"])

    monkeypatch.setattr(AccountManager, "_start_account", fake_start)

    async def main():
        db = Database(DB)
        await db.connect()
        cfg = _config()
        box = SecretBox(cfg.account_encryption_key)
        manager = AccountManager(cfg, db, box)
        await db.create_account(
            "setup-needed",
            "待設定帳號",
            box.encrypt("session"),
            '{"name":"美玲","personality":"活潑"}',
        )

        err = await manager.start("setup-needed")

        assert err == "請先設定群組範圍，再啟動帳號"
        assert start_calls == []
        account = await db.get_account("setup-needed")
        assert account["enabled"] == 0

        await db.create_account(
            "bad-groups",
            "群組資料損壞",
            box.encrypt("session"),
            '{"name":"美玲","personality":"活潑"}',
        )
        await db.update_account(
            "bad-groups", groups="null", setup_complete=1, enabled=0
        )
        err = await manager.start("bad-groups")
        assert err == "請至少選擇一個有效群組，再啟動帳號"
        assert start_calls == []
        bad = await db.get_account("bad-groups")
        assert bad["enabled"] == 0

        await manager.aclose()
        await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


def test_manager_nonexistent_account_returns_not_found():
    """stop/delete 對不存在帳號回傳一致錯誤字串，避免假成功"""
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        cfg = _config()
        box = SecretBox(cfg.account_encryption_key)
        manager = AccountManager(cfg, db, box)

        assert await manager.stop("ghost") == "帳號不存在"
        assert await manager.delete("ghost") == "帳號不存在"

        await manager.aclose()
        await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


def test_worker_notify_status_accepts_sync_and_async_callbacks():
    """_notify_status 應可處理同步和非同步 callback，避免 RuntimeWarning"""
    cfg = _config()
    calls = []

    async def async_cb(*args):
        calls.append(("async", args))

    def sync_cb(*args):
        calls.append(("sync", args))

    worker = AccountWorker(
        account_id="x1",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=None,
        db=None,
        config=cfg,
        managed_ids=set(),
        on_status_change=sync_cb,
        selected_groups=[],
    )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(worker._notify_status("connected", 1, "ok"))
        worker.on_status_change = async_cb
        loop.run_until_complete(worker._notify_status("stopped", None, ""))
    finally:
        loop.close()

    assert calls[0][0] == "sync"
    assert calls[1][0] == "async"


def test_manager_concurrent_stop_and_start_leave_consistent_state():
    """同帳號 stop/start 交錯後，DB enabled 與受管理 worker 必須一致。"""

    class FakeDB:
        def __init__(self):
            self.account = {
                "id": "race",
                "name": "競態帳號",
                "session_key": "unused",
                "setup_complete": 1,
                "groups": "[-5428680940]",
                "enabled": 1,
            }

        async def get_account(self, account_id):
            return dict(self.account) if account_id == "race" else None

        async def update_account(self, account_id, **fields):
            assert account_id == "race"
            self.account.update(fields)
            await asyncio.sleep(0)

        async def list_accounts(self):
            return [dict(self.account)]

    class FakeWorker:
        def __init__(self, stop_started=None, allow_stop=None):
            self.is_running = True
            self._stop_started = stop_started
            self._allow_stop = allow_stop

        async def stop(self):
            if self._stop_started:
                self._stop_started.set()
            if self._allow_stop:
                await self._allow_stop.wait()
            self.is_running = False

    async def main():
        cfg = _config()
        db = FakeDB()
        manager = AccountManager(cfg, db, SecretBox(cfg.account_encryption_key))
        stop_started = asyncio.Event()
        allow_stop = asyncio.Event()
        manager.workers["race"] = FakeWorker(stop_started, allow_stop)

        async def fake_start(account):
            assert account["id"] == "race"
            manager.workers["race"] = FakeWorker()

        manager._start_account = fake_start

        stop_task = asyncio.create_task(manager.stop("race"))
        await stop_started.wait()
        start_task = asyncio.create_task(manager.start("race"))
        await asyncio.sleep(0)
        allow_stop.set()
        stop_result, start_result = await asyncio.gather(stop_task, start_task)

        assert stop_result == ""
        assert start_result == ""
        assert ("race" in manager.workers) is bool(db.account["enabled"])
        assert len(manager.workers) <= 1

        manager.workers.clear()
        await manager.aclose()

    asyncio.run(main())
