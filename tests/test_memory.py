from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from app.memory import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def test_memory_is_group_scoped_and_expires(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            now = int(time.time())
            await store.add(100, 1, "甲", "user", "still here", created_at=now)
            await store.add(
                100,
                2,
                "乙",
                "user",
                "expired",
                created_at=now - 24 * 60 * 60 - 1,
            )
            await store.add(200, 3, "丙", "user", "other group", created_at=now)

            recent = await store.recent_group(100, 20)
            self.assertEqual([item.content for item in recent], ["still here"])

            removed = await store.purge_expired(now=now)
            self.assertEqual(removed, 1)
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


if __name__ == "__main__":
    unittest.main()

