"""SQLite 寫入鎖回歸測試。

線上症狀：bot 選單按「全部靜音／緊急全停」直接回 `OperationalError: database is locked`。

根因：主連線是所有 worker／dashboard／bot 共用的。舊寫法
`cur = await db._c.execute("SELECT ...")` 之後只 fetch、不 close，statement
在 WAL 下維持一個讀快照；之後同一條連線要寫入就得升級交易，SQLite 回
SQLITE_BUSY_SNAPSHOT——這種衝突**不吃 busy_timeout**，等再久也是 locked。

/health 每次被 Railway 定期打一次就留一個游標，所以 bot 的寫入是間歇性失敗。
"""

import asyncio

import aiosqlite
import pytest

from app.database import Database


def test_scalar_closes_cursor_so_writes_still_work(tmp_path):
    """修正後：用 scalar() 讀完，主連線的寫入仍然正常。"""

    async def main():
        path = str(tmp_path / "lock-fixed.db")
        db = Database(path)
        await db.connect()
        await db.create_account("a1", "小小", "session")

        count = await db.scalar("SELECT COUNT(*) FROM accounts")
        assert count == 1

        async with aiosqlite.connect(path, timeout=5) as other:
            await other.execute(
                "INSERT INTO accounts (id, name, session_key) VALUES (?, ?, ?)",
                ("a2", "小天後", "s"),
            )
            await other.commit()

        # 不該拋錯
        await db.update_account("a1", reply_enabled=0)
        enabled = await db.scalar(
            "SELECT reply_enabled FROM accounts WHERE id = 'a1'"
        )
        await db.close()
        assert int(enabled) == 0

    asyncio.run(main())


def test_set_all_accounts_flags_is_one_shot(tmp_path):
    """緊急全停／全部靜音：一次 UPDATE 切換全部帳號。"""

    async def main():
        db = Database(str(tmp_path / "flags.db"))
        await db.connect()
        await db.create_account("a1", "小小", "s1")
        await db.create_account("a2", "小天後", "s2")
        await db.create_account("a3", "RICH", "s3")

        total = await db.set_all_accounts_flags(0, 0)
        assert total == 3
        rows = await db.rows(
            "SELECT reply_enabled, proactive_enabled FROM accounts"
        )
        assert all(int(r[0]) == 0 and int(r[1]) == 0 for r in rows)

        await db.set_all_accounts_flags(1, 1)
        rows = await db.rows(
            "SELECT reply_enabled, proactive_enabled FROM accounts"
        )
        assert all(int(r[0]) == 1 and int(r[1]) == 1 for r in rows)
        await db.close()

    asyncio.run(main())


def test_rows_returns_all_rows_and_closes(tmp_path):
    async def main():
        db = Database(str(tmp_path / "rows.db"))
        await db.connect()
        await db.create_account("a1", "小小", "s1")
        await db.create_account("a2", "小天後", "s2")
        rows = await db.rows("SELECT id FROM accounts ORDER BY id")
        assert [r[0] for r in rows] == ["a1", "a2"]
        # 讀完立刻寫，不該 locked
        await db.update_account("a1", reply_enabled=0)
        await db.close()

    asyncio.run(main())


def test_write_retries_through_transient_lock(tmp_path):
    """主連線寫入撞到 locked 時會退避重試，而不是把錯誤直接丟給使用者。"""

    async def main():
        db = Database(str(tmp_path / "retry.db"))
        await db.connect()
        await db.create_account("a1", "小小", "s1")

        real_execute = db._c.execute
        calls = {"n": 0}

        async def flaky(sql, *args, **kwargs):
            if "UPDATE accounts" in str(sql) and calls["n"] < 2:
                calls["n"] += 1
                raise aiosqlite.OperationalError("database is locked")
            return await real_execute(sql, *args, **kwargs)

        db._c.execute = flaky
        try:
            await db.update_account("a1", reply_enabled=0)
        finally:
            db._c.execute = real_execute

        assert calls["n"] == 2, "應該重試兩次才成功"
        assert int(await db.scalar(
            "SELECT reply_enabled FROM accounts WHERE id = 'a1'"
        )) == 0
        await db.close()

    asyncio.run(main())


def test_write_gives_up_after_attempts(tmp_path):
    """鎖一直不放時，重試用完要拋錯（不能無限等）。"""

    async def main():
        db = Database(str(tmp_path / "giveup.db"))
        await db.connect()
        await db.create_account("a1", "小小", "s1")

        async def always_locked(sql, *args, **kwargs):
            raise aiosqlite.OperationalError("database is locked")

        db._c.execute = always_locked
        with pytest.raises(aiosqlite.OperationalError):
            await db.update_account("a1", reply_enabled=0)
        await db.close()

    asyncio.run(main())


def test_failed_commit_rolls_back_so_connection_is_not_poisoned(tmp_path):
    """commit 撞鎖時必須 rollback：否則殘留交易會讓之後每一次寫入都 locked。"""

    async def main():
        db = Database(str(tmp_path / "poison.db"))
        await db.connect()
        await db.create_account("a1", "小小", "s1")

        real_commit = db._c.commit
        calls = {"n": 0}

        async def flaky_commit():
            calls["n"] += 1
            if calls["n"] <= 6:
                raise aiosqlite.OperationalError("database is locked")
            return await real_commit()

        db._c.commit = flaky_commit
        with pytest.raises(aiosqlite.OperationalError):
            await db.add_message("a1", -1001, 123, "有人", "user", "一則訊息")
        db._c.commit = real_commit

        assert db._c.in_transaction is False, "失敗的 commit 留下未結束交易"
        # 同一條連線之後仍能正常寫入（停止帳號走的就是這條路）
        await db.update_account("a1", enabled=0)
        assert int(await db.scalar("SELECT enabled FROM accounts WHERE id = 'a1'")) == 0
        await db.close()

    asyncio.run(main())


def test_single_account_stop_survives_transient_lock(tmp_path):
    """按「停止」的寫入遇到一次 locked 要自己退避重試，不能把錯誤丟給使用者。"""

    async def main():
        db = Database(str(tmp_path / "stop.db"))
        await db.connect()
        await db.create_account("a1", "小天後", "s1")

        real_commit = db._c.commit
        calls = {"n": 0}

        async def flaky_commit():
            calls["n"] += 1
            if calls["n"] == 1:
                raise aiosqlite.OperationalError("database is locked")
            return await real_commit()

        db._c.commit = flaky_commit
        try:
            await db.update_account("a1", enabled=0)
        finally:
            db._c.commit = real_commit

        assert calls["n"] >= 2, "應先撞鎖再一次成功"
        assert int(await db.scalar("SELECT enabled FROM accounts WHERE id = 'a1'")) == 0
        assert db._c.in_transaction is False
        await db.close()

    asyncio.run(main())


def test_connect_verifies_wal_mode(tmp_path):
    """connect() 要讀回 journal_mode 驗證 WAL 真的生效，並暴露在 diagnostics。"""

    async def main():
        db = Database(str(tmp_path / "wal.db"))
        await db.connect()
        try:
            assert db.journal_mode == "wal", (
                f"WAL 沒生效：{db.journal_mode}"
            )
            diag = db.diagnostics()
            assert diag["wal_ok"] is True
            assert diag["lock_retries"] == 0
            assert diag["lock_failures"] == 0
            assert diag["pragma_warning"] == ""
        finally:
            await db.close()

    asyncio.run(main())


def test_diagnostics_counts_lock_retries(tmp_path):
    """重試次數要可觀測，否則無法判斷 locked 有多頻繁。"""

    async def main():
        db = Database(str(tmp_path / "diag.db"))
        await db.connect()
        await db.create_account("a1", "小小", "s1")

        real_execute = db._c.execute
        calls = {"n": 0}

        async def flaky(sql, *args, **kwargs):
            if "UPDATE accounts" in str(sql) and calls["n"] < 3:
                calls["n"] += 1
                raise aiosqlite.OperationalError("database is locked")
            return await real_execute(sql, *args, **kwargs)

        db._c.execute = flaky
        try:
            await db.update_account("a1", reply_enabled=0)
        finally:
            db._c.execute = real_execute

        diag = db.diagnostics()
        assert diag["lock_retries"] == 3
        assert diag["lock_failures"] == 0
        await db.close()

    asyncio.run(main())


def test_diagnostics_counts_lock_failures(tmp_path):
    async def main():
        db = Database(str(tmp_path / "diag-fail.db"))
        await db.connect()
        await db.create_account("a1", "小小", "s1")

        async def always_locked(sql, *args, **kwargs):
            raise aiosqlite.OperationalError("database is locked")

        db._c.execute = always_locked
        with pytest.raises(aiosqlite.OperationalError):
            await db.update_account("a1", reply_enabled=0)
        assert db.diagnostics()["lock_failures"] == 1
        await db.close()

    asyncio.run(main())
