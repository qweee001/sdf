"""場景表的持久化與 CAS 併發語意（純 DB，不碰 Telegram）。"""

import asyncio
import os

from app.database import Database

DB = "/tmp/sdf_test/scene_test.db"


def _fresh_db_path() -> str:
    os.makedirs("/tmp/sdf_test", exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)
    return DB


def test_open_scene_is_single_per_group():
    path = _fresh_db_path()

    async def main():
        db = Database(path)
        await db.connect()
        try:
            first = await db.open_scene(111, 900, "浴室")
            assert first and first["stage"] == "warm"
            # 同群再開一次：回既有那場，不覆蓋、goal 不換
            again = await db.open_scene(111, 901, "沙發")
            assert again["id"] == first["id"]
            assert again["goal"] == "浴室"
            # 另一個群是獨立的一場
            other = await db.open_scene(222, 900, "車上")
            assert other["id"] != first["id"]
        finally:
            await db.close()

    asyncio.run(main())


def test_transition_cas_only_first_wins():
    path = _fresh_db_path()

    async def main():
        db = Database(path)
        await db.connect()
        try:
            scene = await db.open_scene(333, 900, "浴室")
            sid = scene["id"]
            # 兩個 worker 同時想從 warm 推進：只有一個成功
            results = await asyncio.gather(
                db.transition_scene(sid, "warm", "build", "a"),
                db.transition_scene(sid, "warm", "build", "b"),
            )
            assert sorted(results) == [False, True]
            row = await db.get_open_scene(333)
            assert row["stage"] == "build"
        finally:
            await db.close()

    asyncio.run(main())


def test_close_releases_the_slot_and_sets_cooldown():
    path = _fresh_db_path()

    async def main():
        db = Database(path)
        await db.connect()
        try:
            scene = await db.open_scene(444, 900, "浴室")
            sid = scene["id"]
            assert await db.transition_scene(sid, "warm", "closed", "done")
            assert await db.get_open_scene(444) is None
            # 關場後冷卻時間內不再開新場（由呼叫方判斷）
            assert await db.scene_cooldown_left(444, 1800) > 1700
            # 同一群可以再開一場新的
            new = await db.open_scene(444, 901, "沙發")
            assert new["id"] != sid
        finally:
            await db.close()

    asyncio.run(main())


def test_expire_scenes_closes_stale_rows():
    path = _fresh_db_path()

    async def main():
        db = Database(path)
        await db.connect()
        try:
            scene = await db.open_scene(555, 900, "浴室")
            # 假裝是兩小時前開的
            import time

            await db._c.execute(
                "UPDATE group_scenes SET opened_at = ? WHERE id = ?",
                (time.time() - 7200, scene["id"]),
            )
            await db._commit()
            assert await db.expire_scenes(1800) == 1
            assert await db.get_open_scene(555) is None
        finally:
            await db.close()

    asyncio.run(main())
