import asyncio
import os

from app.database import Database

DB = "/tmp/sdf_test/db_test.db"


def test_crud_full_cycle():
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()

        await db.create_account("a1", "測試帳號", "ciphertext")
        acc = await db.get_account("a1")
        assert acc and acc["name"] == "測試帳號"

        accounts = await db.list_accounts()
        assert len(accounts) == 1

        await db.update_account("a1", enabled=1, tg_user_id=12345)
        acc2 = await db.get_account("a1")
        assert acc2["enabled"] == 1 and acc2["tg_user_id"] == 12345

        await db.add_message("a1", 111, 999, "小王", "user", "你好")
        await db.add_message("a1", 111, 0, "測試帳號", "assistant", "嗨～")
        msgs = await db.get_recent_messages("a1", 111)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "你好"
        assert msgs[1]["role"] == "assistant"

        await db.add_private_message("a1", 555, "阿明", "加個 LINE 嗎")
        priv = await db.get_private_messages("a1", unread_only=True)
        assert len(priv) == 1
        await db.mark_private_message_read(priv[0]["id"])
        assert len(await db.get_private_messages("a1", unread_only=True)) == 0

        await db.touch_activity("a1", 111, "proactive")
        assert await db.last_activity("a1", 111, "proactive") > 0

        stats = await db.stats_for("a1")
        assert stats["sent"] == 1

        await db.delete_account("a1")
        assert await db.get_account("a1") is None
        assert len(await db.get_recent_messages("a1", 111)) == 0
        await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


def test_groups_column_roundtrip():
    """指定群組：groups 欄位可存讀（JSON list of int）"""
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        await db.create_account("g1", "群組帳號", "ciphertext")
        acc = await db.get_account("g1")
        # 新帳號預設尚未完成群組設定
        assert acc["setup_complete"] == 0
        # 預設空（自動所有群）
        assert acc.get("groups") in (None, "", "[]", "null")
        await db.update_account("g1", groups='[-100123, -100456]')
        acc2 = await db.get_account("g1")
        import json
        assert json.loads(acc2["groups"]) == [-100123, -100456]
        # list_accounts 也回傳 groups
        lst = await db.list_accounts()
        assert lst[0]["groups"] == '[-100123, -100456]'
        await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


def test_groups_migration_on_existing_db():
    """舊庫升級：原本沒有 groups 欄位的 DB，連線時自動補欄位"""
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        import aiosqlite
        # 手動建一個「舊版」accounts（無 groups 欄位）
        conn = await aiosqlite.connect(DB)
        await conn.execute("DROP TABLE IF EXISTS accounts")
        await conn.execute(
            "CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, "
            "session_key TEXT, tg_user_id INTEGER, tg_username TEXT, "
            "persona TEXT, enabled INTEGER DEFAULT 0, created_at REAL, updated_at REAL)"
        )
        await conn.execute(
            "INSERT INTO accounts (id, name, session_key, enabled) VALUES ('legacy', '既有帳號', 'x', 1)"
        )
        await conn.commit()
        await conn.close()
        # 重新連線應補上欄位；既有帳號視為已設定，新帳號仍需設定
        db2 = Database(DB)
        await db2.connect()
        legacy = await db2.get_account("legacy")
        assert "groups" in legacy
        assert legacy["setup_complete"] == 1

        await db2.create_account("new", "新帳號", "x")
        new = await db2.get_account("new")
        assert new["setup_complete"] == 0
        await db2.update_account("new", groups="[123]", setup_complete=1)
        updated = await db2.get_account("new")
        assert updated["groups"] == "[123]"
        assert updated["setup_complete"] == 1
        await db2.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


def test_memory_cap_200():
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        await db.create_account("b", "x", "y")
        for i in range(220):
            await db.add_message("b", 1, 0, "u", "user", f"msg {i}")
        msgs = await db.get_recent_messages("b", 1, limit=50)
        assert len(msgs) == 50
        assert msgs[-1]["content"] == "msg 219"
        await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
