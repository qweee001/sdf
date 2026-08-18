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
