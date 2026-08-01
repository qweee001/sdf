from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.media_types import utc_day_key
from app.memory import MemoryStore
from tests.test_group_coordination import _account


class ProactiveRetentionTests(unittest.TestCase):
    def test_purge_removes_only_expired_noncurrent_proactive_state(self) -> None:
        async def scenario(path: str) -> None:
            now = 1_800_000_000
            old = now - 8 * 24 * 60 * 60 - 1
            recent = now - 60
            store = MemoryStore(path, ttl_hours=1)
            try:
                await store.open()
                await store.create_account(_account("alpha", 501001))
                db = store._connection()
                await db.executemany(
                    """
                    INSERT INTO proactive_usage (
                        account_id, group_id, day_key, used_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "alpha",
                            -1001,
                            utc_day_key(now - 24 * 60 * 60),
                            1,
                            old,
                        ),
                        (
                            "alpha",
                            -1002,
                            utc_day_key(now - 24 * 60 * 60),
                            1,
                            recent,
                        ),
                        ("alpha", -1001, utc_day_key(now), 2, old),
                    ],
                )
                await db.executemany(
                    """
                    INSERT INTO proactive_group_state (
                        group_id, last_activity_at, last_proactive_at,
                        lease_owner, lease_token, lease_until
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (-2001, old, old, "", "", 0),
                        (-2002, recent, 0, "", "", 0),
                        (-2003, 0, 0, "alpha", "a" * 32, now + 60),
                        (-2004, 0, now - 2 * 24 * 60 * 60, "", "", 0),
                    ],
                )
                await db.commit()

                self.assertEqual(await store.purge_expired(now=now), 2)
                cursor = await db.execute(
                    """
                    SELECT group_id, day_key FROM proactive_usage
                    ORDER BY group_id, day_key
                    """
                )
                usage = [tuple(row) for row in await cursor.fetchall()]
                await cursor.close()
                self.assertEqual(
                    usage,
                    [
                        (-1002, utc_day_key(now - 24 * 60 * 60)),
                        (-1001, utc_day_key(now)),
                    ],
                )
                cursor = await db.execute(
                    "SELECT group_id FROM proactive_group_state ORDER BY group_id"
                )
                states = [int(row["group_id"]) for row in await cursor.fetchall()]
                await cursor.close()
                self.assertEqual(states, [-2004, -2003, -2002])
                cooldown = await store.claim_proactive_lease(
                    "alpha",
                    -2004,
                    idle_seconds=0,
                    cooldown_seconds=7 * 24 * 60 * 60,
                    daily_limit=10,
                    now=now,
                )
                self.assertFalse(cooldown.allowed)
                self.assertEqual(cooldown.reason, "cooldown")
            finally:
                await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


if __name__ == "__main__":
    unittest.main()
