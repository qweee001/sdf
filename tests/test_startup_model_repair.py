from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.memory import MemoryStore
from tests.test_group_coordination import _account


class StartupModelRepairTests(unittest.TestCase):
    def test_only_exact_retired_openrouter_model_is_repaired_and_audited(
        self,
    ) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            try:
                await store.open()
                exact = _account("exact", 401001).with_updates(
                    ai_model="x-ai/grok-4.1-fast"
                )
                custom = _account("custom", 401002).with_updates(
                    ai_model="gpt-5.6-luna"
                )
                trailing_slash = _account("trailing-slash", 401003).with_updates(
                    ai_base_url="https://openrouter.ai/api/v1/",
                    ai_model="x-ai/grok-4.1-fast",
                )
                uppercase_host = _account("uppercase-host", 401004).with_updates(
                    ai_base_url="https://OPENROUTER.AI:443/api/v1/",
                    ai_model="x-ai/grok-4.1-fast",
                )
                other_base = _account("other-base", 401005).with_updates(
                    ai_base_url="https://openrouter.ai/api/v2",
                    ai_model="x-ai/grok-4.1-fast",
                )
                await store.create_account(exact)
                await store.create_account(custom)
                await store.create_account(trailing_slash)
                await store.create_account(uppercase_host)
                await store.create_account(other_base)
            finally:
                await store.close()

            repaired = MemoryStore(path, ttl_hours=24)
            try:
                await repaired.open()
                self.assertEqual(
                    (await repaired.get_account("exact")).ai_model,
                    "x-ai/grok-4.3",
                )
                self.assertEqual((await repaired.get_account("exact")).revision, 2)
                self.assertEqual(
                    (await repaired.get_account("custom")).ai_model,
                    "gpt-5.6-luna",
                )
                self.assertEqual(
                    (await repaired.get_account("trailing-slash")).ai_model,
                    "x-ai/grok-4.3",
                )
                self.assertEqual(
                    (await repaired.get_account("uppercase-host")).ai_model,
                    "x-ai/grok-4.3",
                )
                self.assertEqual(
                    (await repaired.get_account("other-base")).ai_model,
                    "x-ai/grok-4.1-fast",
                )
                cursor = await repaired._connection().execute(
                    """
                    SELECT account_id, fields FROM audit_log
                    WHERE action='repair_retired_grok_model'
                    ORDER BY id
                    """
                )
                rows = await cursor.fetchall()
                await cursor.close()
                self.assertEqual(
                    [str(row["account_id"]) for row in rows],
                    ["exact", "trailing-slash", "uppercase-host"],
                )
                self.assertTrue(
                    all(json.loads(row["fields"]) == ["ai_model"] for row in rows)
                )
            finally:
                await repaired.close()

            reopened = MemoryStore(path, ttl_hours=24)
            try:
                await reopened.open()
                self.assertEqual((await reopened.get_account("exact")).revision, 2)
                cursor = await reopened._connection().execute(
                    """
                    SELECT COUNT(*) AS total FROM audit_log
                    WHERE action='repair_retired_grok_model'
                    """
                )
                row = await cursor.fetchone()
                await cursor.close()
                self.assertEqual(int(row["total"]), 3)
            finally:
                await reopened.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


if __name__ == "__main__":
    unittest.main()
