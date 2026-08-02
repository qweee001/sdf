from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from app.memory import GroupActivityProfile, MemoryStore
from app.worker import AccountWorker


class AdaptivePolicyTests(unittest.TestCase):
    @staticmethod
    def profile(
        recent_messages: int,
        recent_participants: int,
        trailing_messages: int,
        trailing_participants: int,
    ) -> GroupActivityProfile:
        return GroupActivityProfile(
            recent_messages=recent_messages,
            recent_participants=recent_participants,
            trailing_messages=trailing_messages,
            trailing_participants=trailing_participants,
            latest_message_at=None,
        )

    def test_busy_group_reduces_replies_and_proactive_frequency(self) -> None:
        policy = AccountWorker._activity_policy_from_profile(
            0.80,
            self.profile(14, 7, 35, 10),
        )
        self.assertEqual(policy.mode, "very_busy")
        self.assertAlmostEqual(policy.reply_probability, 0.16)
        self.assertEqual(policy.proactive_idle_multiplier, 2.0)
        self.assertEqual(policy.proactive_cooldown_multiplier, 2.0)

    def test_quiet_group_increases_replies_and_proactive_frequency(self) -> None:
        policy = AccountWorker._activity_policy_from_profile(
            0.35,
            self.profile(1, 1, 2, 1),
        )
        self.assertEqual(policy.mode, "quiet")
        self.assertAlmostEqual(policy.reply_probability, 0.63)
        self.assertEqual(policy.proactive_idle_multiplier, 0.5)
        self.assertEqual(policy.proactive_cooldown_multiplier, 0.6)

    def test_reply_probability_never_exceeds_one(self) -> None:
        policy = AccountWorker._activity_policy_from_profile(
            1.0,
            self.profile(0, 0, 0, 0),
        )
        self.assertEqual(policy.reply_probability, 1.0)

    def test_multiple_accounts_preserve_the_group_level_probability(self) -> None:
        policy = AccountWorker._activity_policy_from_profile(
            1.0,
            self.profile(14, 7, 35, 10),
            managed_account_count=4,
        )
        combined = 1.0 - (1.0 - policy.reply_probability) ** 4
        self.assertAlmostEqual(combined, 0.20)


class ActivityProfileTests(unittest.TestCase):
    def test_profile_counts_only_human_rows_in_each_window(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            now = int(time.time())
            await store.add(
                "alpha", -1001, 1, "A", "user", "recent", created_at=now - 30
            )
            await store.add(
                "alpha", -1001, 2, "B", "user", "recent", created_at=now - 120
            )
            await store.add(
                "alpha", -1001, 1, "A", "user", "trailing", created_at=now - 600
            )
            await store.add(
                "alpha", -1001, 99, "bot", "assistant", "ignored", created_at=now - 10
            )
            await store.add(
                "beta", -1001, 3, "C", "user", "other account", created_at=now - 10
            )
            await store.add(
                "alpha", -1002, 4, "D", "user", "other group", created_at=now - 10
            )

            profile = await store.group_activity_profile(
                "alpha",
                -1001,
                now=now,
            )
            self.assertEqual(profile.recent_messages, 2)
            self.assertEqual(profile.recent_participants, 2)
            self.assertEqual(profile.trailing_messages, 3)
            self.assertEqual(profile.trailing_participants, 2)
            self.assertEqual(profile.latest_message_at, now - 30)
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


if __name__ == "__main__":
    unittest.main()
