import asyncio
import json

import pytest

from app.config import load_settings
from app.crypto import SecretBox
from app.database import Database
from app.live_test import LiveTestError
from app.manager import AccountManager


def test_manager_injects_shared_gate_into_production_worker(monkeypatch, tmp_path):
    captured = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.is_running = True

        async def start(self):
            return None

        async def stop(self):
            return None

    monkeypatch.setattr("app.manager.AccountWorker", FakeWorker)

    async def main():
        db = Database(str(tmp_path / "manager-gate.db"))
        await db.connect()
        config = load_settings()
        box = SecretBox(config.account_encryption_key)
        manager = AccountManager(config, db, box)
        account = {
            "id": "acct-gated",
            "session_key": box.encrypt("session"),
            "persona": "",
            "groups": json.dumps([-100777]),
        }

        await manager._start_account(account)

        assert captured["outbound_gate"] is manager.live_test.outbound_gate
        await manager.aclose()
        await db.close()

    asyncio.run(main())


def test_start_all_and_stop_share_account_lifecycle_lock(monkeypatch, tmp_path):
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    instances = []

    class BlockingWorker:
        def __init__(self, **kwargs):
            self.is_running = False
            instances.append(self)

        async def start(self):
            start_entered.set()
            await release_start.wait()
            self.is_running = True

        async def stop(self):
            self.is_running = False

    monkeypatch.setattr("app.manager.AccountWorker", BlockingWorker)

    async def main():
        db = Database(str(tmp_path / "manager-lifecycle.db"))
        await db.connect()
        config = load_settings()
        box = SecretBox(config.account_encryption_key)
        manager = AccountManager(config, db, box)
        await db.create_account(
            "acct-race", "race", box.encrypt("session"), ""
        )
        await db.update_account(
            "acct-race",
            groups=json.dumps([-100777]),
            setup_complete=1,
            enabled=1,
        )

        start_task = asyncio.create_task(manager.start_all())
        await start_entered.wait()
        stop_task = asyncio.create_task(manager.stop("acct-race"))
        await asyncio.sleep(0.05)

        assert stop_task.done() is False
        release_start.set()
        await asyncio.gather(start_task, stop_task)

        account = await db.get_account("acct-race")
        assert account["enabled"] == 0
        assert "acct-race" not in manager.workers
        assert instances and instances[0].is_running is False
        await manager.aclose()
        await db.close()

    asyncio.run(main())


def test_update_failure_survives_restart_and_blocks_all_start_paths(monkeypatch, tmp_path):
    account_ids = [
        "2ce525dfb0d4",
        "faa9a202f96e",
        "038632e4395b",
        "e63e27a4340d",
    ]
    group_id = -5428680940
    started_workers = []

    class TrackingWorker:
        def __init__(self, **kwargs):
            self.is_running = False
            started_workers.append(self)

        async def start(self):
            self.is_running = True

        async def stop(self):
            self.is_running = False

    monkeypatch.setattr("app.manager.AccountWorker", TrackingWorker)

    async def seed(db, box):
        for account_id, age in zip(account_ids, (21, 25, 29, 34), strict=True):
            persona = json.dumps(
                {"name": account_id, "gender": "女", "age": age},
                ensure_ascii=False,
            )
            await db.create_account(
                account_id, account_id, box.encrypt("session"), persona
            )
            await db.update_account(
                account_id,
                groups=json.dumps([group_id]),
                setup_complete=1,
                enabled=1,
            )

    async def main():
        path = str(tmp_path / "persistent-lockdown.db")
        config = load_settings()
        box = SecretBox(config.account_encryption_key)
        first_db = Database(path)
        await first_db.connect()
        await seed(first_db, box)
        assert await first_db.create_live_test_run(
            run_id="restart-lockdown",
            account_ids=account_ids,
            group_id=group_id,
            duration_seconds=3600,
            event_cap=40,
            schedule=[
                {
                    "event_id": f"restart-lockdown-{index}",
                    "account_id": account_ids[index % 4],
                    "kind": "text",
                }
                for index in range(30)
            ],
            started_at=1_000.0,
        )
        first_manager = AccountManager(config, first_db, box)
        real_first_update = first_db.update_account

        async def fail_first_update(account_id, **fields):
            if account_id == account_ids[0]:
                raise RuntimeError("injected update_account failure")
            return await real_first_update(account_id, **fields)

        first_db.update_account = fail_first_update
        await first_manager.live_test.reconcile()
        persisted = await first_db.get_live_test_run("restart-lockdown")
        assert persisted["status"] == "needs_reconciliation"
        assert (await first_db.get_account(account_ids[0]))["enabled"] == 1
        await first_db.close()

        restarted_db = Database(path)
        await restarted_db.connect()
        restarted = AccountManager(config, restarted_db, box)
        real_restart_update = restarted_db.update_account

        async def keep_failing(account_id, **fields):
            if account_id == account_ids[0]:
                raise RuntimeError("still cannot persist disable")
            return await real_restart_update(account_id, **fields)

        restarted_db.update_account = keep_failing
        await restarted.live_test.reconcile()
        still_locked = await restarted_db.get_live_test_run("restart-lockdown")
        assert still_locked["status"] == "needs_reconciliation"

        assert "reconciliation" in await restarted.start(account_ids[0])
        assert "reconciliation" in await restarted.start_all()
        assert restarted.workers == {}
        assert started_workers == []
        with pytest.raises(LiveTestError, match="reconciliation"):
            await restarted.start_live_test({})
        permit = await restarted.live_test.outbound_gate.reserve(
            account_id=account_ids[0],
            group_id=group_id,
            kind="text",
            event_id="scripted",
        )
        assert permit.allowed is False

        restarted_db.update_account = real_restart_update
        await restarted.live_test.reconcile()
        reconciled = await restarted_db.get_live_test_run("restart-lockdown")
        assert reconciled["status"] == "failed"
        for account_id in account_ids:
            assert (await restarted_db.get_account(account_id))["enabled"] == 0
        assert restarted.workers == {}

        await restarted.aclose()
        await restarted_db.close()

    asyncio.run(main())
