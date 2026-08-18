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


def test_worker_no_selected_groups_means_all():
    """沒指定群組（空）→ 任何群都處理（向下相容）"""
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        worker = _make_worker(db, "w2", [])
        await worker.on_message(_FakeEvent(-9999))
        assert len(await db.get_recent_messages("w2", -9999)) >= 1
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
        # 不存在的帳號
        assert await manager.save_groups("nope", [1]) == "帳號不存在"
        await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
