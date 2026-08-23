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

        await db.create_account("a2", "第二帳號", "ciphertext2")
        await db.add_message("a2", 111, 0, "第二帳號", "assistant", "不同說法")
        recent_group_replies = await db.get_recent_group_replies(111, limit=10)
        assert recent_group_replies == ["不同說法", "嗨～"]

        assert await db.reserve_media_budget(
            "a1", "image", 0.60, 1.00, day="2026-08-23"
        ) is True
        assert await db.reserve_media_budget(
            "a2", "video", 0.50, 1.00, day="2026-08-23"
        ) is False
        assert await db.media_spend_total("2026-08-23") == 0.60

        await db.add_private_message("a1", 555, "阿明", "加個 LINE 嗎")
        priv = await db.get_private_messages("a1", unread_only=True)
        assert len(priv) == 1
        await db.mark_private_message_read(priv[0]["id"])
        assert len(await db.get_private_messages("a1", unread_only=True)) == 0

        assert await db.claim_message_response(111, 9001, "a1") is True
        assert await db.claim_message_response(111, 9001, "a2") is False
        assert await db.claim_proactive_slot(222, 7, "a1", 60) is True
        assert await db.claim_proactive_slot(222, 7, "a2", 60) is False

        await db.touch_activity("a1", 111, "proactive")
        assert await db.last_activity("a1", 111, "proactive") > 0
        assert await db.last_group_activity(111, "proactive") > 0

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


def test_media_budget_rejects_nan_and_infinity():
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        assert await db.reserve_media_budget(
            "a1", "image", 0.03, float("nan"), day="2026-08-23"
        ) is False
        assert await db.reserve_media_budget(
            "a1", "image", float("inf"), 2.0, day="2026-08-23"
        ) is False
        assert await db.media_spend_total("2026-08-23") == 0.0
        await db.close()

    asyncio.run(main())


def test_media_budget_is_atomic_across_database_connections():
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        first = Database(DB)
        second = Database(DB)
        await first.connect()
        await second.connect()
        start = asyncio.Event()

        async def reserve(db, account_id):
            await start.wait()
            return await db.reserve_media_budget(
                account_id,
                "video",
                0.60,
                1.00,
                day="2026-08-23",
            )

        tasks = [
            asyncio.create_task(reserve(first, "a1")),
            asyncio.create_task(reserve(second, "a2")),
        ]
        start.set()
        results = await asyncio.gather(*tasks)

        assert results.count(True) == 1
        assert await first.media_spend_total("2026-08-23") == 0.60
        await first.close()
        await second.close()

    asyncio.run(main())


def test_group_text_claim_is_atomic_across_database_connections():
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        first = Database(DB)
        second = Database(DB)
        await first.connect()
        await second.connect()
        results = await asyncio.gather(
            first.claim_group_text(111, "今晚 要不要聊聊？", "a1"),
            second.claim_group_text(111, "今晚要不要聊聊", "a2"),
        )
        assert results.count(True) == 1
        await first.close()
        await second.close()

    asyncio.run(main())


def test_managed_followup_claim_combines_event_and_group_cooldown():
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        first = Database(DB)
        second = Database(DB)
        await first.connect()
        await second.connect()

        same_event = await asyncio.gather(
            first.claim_managed_followup(111, 501, "a1", 600),
            second.claim_managed_followup(111, 501, "a2", 600),
        )
        assert same_event.count(True) == 1

        # 同一冷卻槽內，即使是另一個主動消息也不能再接話。
        assert await first.claim_managed_followup(111, 502, "a3", 600) is False
        # 不同群組有獨立冷卻。
        assert await second.claim_managed_followup(222, 502, "a3", 600) is True

        await first.close()
        await second.close()

    asyncio.run(main())


def test_managed_followup_two_phase_reservation_and_release():
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        first = Database(DB)
        second = Database(DB)
        await first.connect()
        await second.connect()

        assert await first.reserve_managed_followup(111, 601, "a1") is True
        assert await second.reserve_managed_followup(111, 601, "a2") is False
        assert await second.reserve_managed_followup(111, 602, "a2") is False

        await first.release_managed_followup(111, 601, "a1")
        assert await second.reserve_managed_followup(111, 602, "a2") is True
        assert await second.complete_managed_followup(111, 602, "a2") is True

        # 成功發送後才建立群級冷卻；另一事件在冷卻內不能認領。
        assert await first.reserve_managed_followup(111, 603, "a1") is False
        # 其他群不受影響。
        assert await first.reserve_managed_followup(222, 603, "a1") is True

        await first.close()
        await second.close()

    asyncio.run(main())
