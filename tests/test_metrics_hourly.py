"""逐小時呼叫統計：分桶、排序、壞值夾取。"""

import asyncio
import os
import time

from app.database import Database

DB = "/tmp/sdf_test/hourly_test.db"


def test_hourly_buckets_split_by_hour():
    os.makedirs("/tmp/sdf_test", exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        try:
            now = time.time()
            for offset, latency in ((0, 100), (0, 300), (3600 * 3, 50)):
                await db._c.execute(
                    "INSERT INTO ai_calls (account_id, purpose, model, latency_ms, cost_status, at) "
                    "VALUES ('a1', 'reply', 'm', ?, 'unmeasured', ?)",
                    (latency, now - offset),
                )
            await db._commit()
            out = await db.ai_call_hourly(6)
            assert out["hours"] == 6
            assert len(out["buckets"]) == 2, out
            # 由舊到新
            assert out["buckets"][0]["calls"] == 1
            assert out["buckets"][1]["calls"] == 2
            assert out["buckets"][1]["avg_latency_ms"] == 200.0
        finally:
            await db.close()

    asyncio.run(main())


def test_hourly_hours_are_clamped():
    os.makedirs("/tmp/sdf_test", exist_ok=True)

    async def main():
        db = Database(DB)
        await db.connect()
        try:
            assert (await db.ai_call_hourly(999))["hours"] == 168
            assert (await db.ai_call_hourly(0))["hours"] == 1
        finally:
            await db.close()

    asyncio.run(main())
