from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from app.account import AccountRecord
from app.memory import MemoryStore


GROUP_ID = -5428680940
ACCOUNT_IDS = ("stress-a", "stress-b", "stress-c", "stress-d")


def account_record(account_id: str, telegram_id: int) -> AccountRecord:
    now = int(time.time())
    return AccountRecord(
        id=account_id,
        label=account_id,
        session_ciphertext="encrypted-session",
        session_fingerprint=f"fingerprint-{account_id}",
        telegram_user_id=telegram_id,
        telegram_name=account_id,
        enabled=True,
        gender="female",
        stage="old_member",
        style="自然、穩重",
        task_name="四帳號壓力測試",
        task_info="",
        ai_base_url="https://openrouter.ai/api/v1",
        ai_model="openai/gpt-5.6-sol",
        ai_api_key_ciphertext="",
        group_reply_probability=1.0,
        reply_on_mention=True,
        reply_on_reply=True,
        typing_delay_min_seconds=0,
        typing_delay_max_seconds=0,
        proactive_enabled=True,
        proactive_idle_minutes=1,
        proactive_min_interval_minutes=1,
        proactive_max_interval_minutes=1,
        max_proactive_per_day=200,
        all_groups=False,
        group_ids=frozenset({GROUP_ID}),
        revision=1,
        created_at=now,
        updated_at=now,
    )


class ExtremeCoordinationTests(unittest.TestCase):
    def test_four_connections_keep_one_reply_claim_under_heavy_contention(
        self,
    ) -> None:
        async def scenario(path: str) -> None:
            stores = [MemoryStore(path, ttl_hours=24) for _ in ACCOUNT_IDS]
            opened: list[MemoryStore] = []
            try:
                await stores[0].open()
                opened.append(stores[0])
                for index, account_id in enumerate(ACCOUNT_IDS):
                    await stores[0].create_account(
                        account_record(account_id, 990_000 + index)
                    )
                for store in stores[1:]:
                    await store.open()
                    opened.append(store)

                for round_id in range(250):
                    claim_key = hashlib.sha256(
                        f"stress-claim-{round_id}".encode()
                    ).hexdigest()
                    results = await asyncio.gather(
                        *(
                            store.claim_group_reply(
                                account_id,
                                GROUP_ID,
                                claim_key,
                            )
                            for store, account_id in zip(stores, ACCOUNT_IDS)
                        )
                    )
                    self.assertEqual(sum(results), 1)
            finally:
                await asyncio.gather(
                    *(store.close() for store in opened),
                    return_exceptions=True,
                )

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_four_connections_preserve_all_messages_under_write_pressure(
        self,
    ) -> None:
        async def scenario(path: str) -> None:
            stores = [MemoryStore(path, ttl_hours=24) for _ in ACCOUNT_IDS]
            opened: list[MemoryStore] = []
            try:
                await stores[0].open()
                opened.append(stores[0])
                for index, account_id in enumerate(ACCOUNT_IDS):
                    await stores[0].create_account(
                        account_record(account_id, 991_000 + index)
                    )
                for store in stores[1:]:
                    await store.open()
                    opened.append(store)

                async def writer(
                    store: MemoryStore,
                    account_id: str,
                    sender_base: int,
                ) -> None:
                    for offset in range(250):
                        await store.add(
                            account_id,
                            GROUP_ID,
                            sender_base + offset,
                            f"member-{offset}",
                            "user",
                            f"stress-message-{account_id}-{offset}",
                        )

                await asyncio.gather(
                    *(
                        writer(store, account_id, 1_000_000 + index * 1_000)
                        for index, (store, account_id) in enumerate(
                            zip(stores, ACCOUNT_IDS)
                        )
                    )
                )
                statistics = await stores[0].statistics()
                self.assertEqual(statistics["message_count"], 1_000)
                self.assertEqual(statistics["group_count"], 1)
            finally:
                await asyncio.gather(
                    *(store.close() for store in opened),
                    return_exceptions=True,
                )

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


if __name__ == "__main__":
    unittest.main()
