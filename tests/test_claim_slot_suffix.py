"""飢餓帳號的專屬 claim key（2026-11-08 10:00–10:17 桃花源）。

破口：同一格 proactive slot 只有一個人搶得到，四條 worker 迴圈在重啟後相位對齊，
誰先 tick 誰贏。小小九次搶格全敗、連生成都輪不到（band 統計 0 筆），
但它的飢餓閘門（starved）只放行隨機閘門，擋不到這道。

修法：飢餓時用自己專屬的 claim key，並把冷卻壓到 15 秒，讓它插得回場；
非飢餓帳號維持原行為（同一格只發一則）。
"""
import asyncio
import os

from app.database import Database

DB = "/tmp/sdf_test/db_claim_suffix.db"


def test_starved_account_can_claim_its_own_slot():
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()

        # 一般路徑：同一格只發一則
        assert await db.claim_proactive_slot(222, 7, "a1", 60) is True
        assert await db.claim_proactive_slot(222, 7, "a2", 60) is False

        # 飢餓帳號帶自己的 key：插得進同一格
        assert await db.claim_proactive_slot(222, 7, "a2", 15, "222") is True
        # 自己重複搶同一格仍要失敗
        assert await db.claim_proactive_slot(222, 7, "a2", 15, "222") is False
        # 沒歸還前別的飢餓帳號也搶不到（key 只屬於自己）
        assert await db.claim_proactive_slot(222, 7, "a3", 15, "333") is True

        # 歸還帶 suffix 的窗口，之後同一格可以再被同一個帳號搶
        assert await db.release_proactive_slot(222, 7, "a2", "222") is True
        assert await db.claim_proactive_slot(222, 7, "a2", 15, "222") is True
        # 一般 key 沒有被動到
        assert await db.release_proactive_slot(222, 7, "a1") is True

        await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
