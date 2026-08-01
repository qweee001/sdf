from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.account import AccountRecord
from app.memory import MemoryStore
from app.media_types import AccountMediaSettings, MediaFeatureSettings


V2_ACCOUNT_COLUMNS = (
    "id",
    "label",
    "session_ciphertext",
    "session_fingerprint",
    "telegram_user_id",
    "telegram_name",
    "enabled",
    "gender",
    "stage",
    "style",
    "task_name",
    "task_info",
    "ai_base_url",
    "ai_model",
    "ai_api_key_ciphertext",
    "group_reply_probability",
    "reply_on_mention",
    "reply_on_reply",
    "typing_delay_min_seconds",
    "typing_delay_max_seconds",
    "proactive_enabled",
    "proactive_idle_minutes",
    "proactive_min_interval_minutes",
    "proactive_max_interval_minutes",
    "max_proactive_per_day",
    "all_groups",
    "group_ids",
    "revision",
    "created_at",
    "updated_at",
)


def account_record(account_id: str = "alpha", revision: int = 1) -> AccountRecord:
    now = int(time.time())
    return AccountRecord(
        id=account_id,
        label="測試帳號",
        session_ciphertext="encrypted-session",
        session_fingerprint=f"fingerprint-{account_id}",
        telegram_user_id=1000 if account_id == "alpha" else 2000,
        telegram_name="測試",
        enabled=False,
        gender="male",
        stage="observer",
        style="自然",
        task_name="測試任務",
        task_info="測試內容",
        ai_base_url="https://api.example.com/v1",
        ai_model="model-a",
        ai_api_key_ciphertext="",
        group_reply_probability=0.35,
        reply_on_mention=True,
        reply_on_reply=True,
        typing_delay_min_seconds=1.0,
        typing_delay_max_seconds=2.0,
        proactive_enabled=True,
        proactive_idle_minutes=15,
        proactive_min_interval_minutes=25,
        proactive_max_interval_minutes=60,
        max_proactive_per_day=12,
        all_groups=True,
        group_ids=frozenset(),
        revision=revision,
        created_at=now,
        updated_at=now,
    )


def rebuild_as_v2_account_schema(
    path: str,
    *,
    keep_blocked_terms: bool = False,
) -> None:
    columns = list(V2_ACCOUNT_COLUMNS)
    if keep_blocked_terms:
        columns.append("blocked_terms")
    selected = ", ".join(f'"{column}"' for column in columns)
    optional_policy_column = (
        ", blocked_terms TEXT NOT NULL DEFAULT '[]'"
        if keep_blocked_terms
        else ""
    )

    db = sqlite3.connect(path)
    db.execute("ALTER TABLE accounts RENAME TO accounts_v3_source")
    db.execute(
        f"""
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            session_ciphertext TEXT NOT NULL,
            session_fingerprint TEXT NOT NULL UNIQUE,
            telegram_user_id INTEGER NOT NULL UNIQUE,
            telegram_name TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            gender TEXT NOT NULL,
            stage TEXT NOT NULL,
            style TEXT NOT NULL,
            task_name TEXT NOT NULL,
            task_info TEXT NOT NULL,
            ai_base_url TEXT NOT NULL,
            ai_model TEXT NOT NULL,
            ai_api_key_ciphertext TEXT NOT NULL,
            group_reply_probability REAL NOT NULL,
            reply_on_mention INTEGER NOT NULL,
            reply_on_reply INTEGER NOT NULL,
            typing_delay_min_seconds REAL NOT NULL,
            typing_delay_max_seconds REAL NOT NULL,
            proactive_enabled INTEGER NOT NULL,
            proactive_idle_minutes INTEGER NOT NULL,
            proactive_min_interval_minutes INTEGER NOT NULL,
            proactive_max_interval_minutes INTEGER NOT NULL,
            max_proactive_per_day INTEGER NOT NULL,
            all_groups INTEGER NOT NULL,
            group_ids TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
            {optional_policy_column}
        )
        """
    )
    db.execute(
        f"""
        INSERT INTO accounts ({selected})
        SELECT {selected} FROM accounts_v3_source
        """
    )
    db.execute("DROP TABLE accounts_v3_source")
    db.execute("PRAGMA user_version=2")
    db.commit()
    db.close()


class MemoryStoreTests(unittest.TestCase):
    def test_memory_is_account_and_group_scoped_and_expires(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            now = int(time.time())
            await store.add("alpha", 100, 1, "甲", "user", "alpha", created_at=now)
            await store.add("beta", 100, 2, "乙", "user", "beta", created_at=now)
            await store.add(
                "alpha",
                100,
                3,
                "丙",
                "user",
                "expired",
                created_at=now - 24 * 60 * 60 - 1,
            )
            await store.add("alpha", 200, 4, "丁", "user", "other", created_at=now)

            recent = await store.recent_group("alpha", 100, 20)
            self.assertEqual([item.content for item in recent], ["alpha"])
            self.assertEqual(
                await store.statistics("alpha"),
                {"message_count": 2, "group_count": 2},
            )
            self.assertEqual(await store.clear_all("alpha"), 3)
            self.assertEqual(
                await store.statistics("beta"),
                {"message_count": 1, "group_count": 1},
            )
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_conversation_log_is_recent_bounded_ordered_and_redacted(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            now = int(time.time())
            for index in range(105):
                await store.add(
                    "alpha",
                    -1001,
                    10000 + index,
                    f"成員 {index:03d}",
                    "user" if index % 2 == 0 else "assistant",
                    f"message-{index:03d}",
                    created_at=now - 104 + index,
                )
            await store.add(
                "alpha",
                -1001,
                999,
                "過期成員",
                "user",
                "expired",
                created_at=now - 24 * 60 * 60 - 1,
            )
            await store.add(
                "beta",
                -1001,
                888,
                "其他帳號",
                "user",
                "other-account-secret",
                created_at=now,
            )
            await store.add(
                "alpha",
                -2002,
                777,
                "其他群組",
                "user",
                "other-group-secret",
                created_at=now,
            )

            entries = await store.conversation_log("alpha", -1001, 100)
            self.assertEqual(len(entries), 100)
            self.assertEqual(entries[0].content, "message-005")
            self.assertEqual(entries[-1].content, "message-104")
            self.assertEqual(
                [entry.created_at for entry in entries],
                sorted(entry.created_at for entry in entries),
            )
            self.assertTrue(
                all(
                    set(entry.__dict__)
                    == {"created_at", "sender_name", "role", "content"}
                    for entry in entries
                )
            )
            self.assertTrue(all(not hasattr(entry, "sender_id") for entry in entries))
            self.assertNotIn(
                "other-account-secret",
                [entry.content for entry in entries],
            )
            self.assertNotIn(
                "other-group-secret",
                [entry.content for entry in entries],
            )
            self.assertNotIn("expired", [entry.content for entry in entries])

            latest = await store.conversation_log("alpha", -1001, 3)
            self.assertEqual(
                [entry.content for entry in latest],
                ["message-102", "message-103", "message-104"],
            )
            for invalid_limit in (0, 101, True):
                with self.assertRaises(ValueError):
                    await store.conversation_log("alpha", -1001, invalid_limit)
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_old_message_schema_migrates_transactionally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "legacy.db")
            db = sqlite3.connect(path)
            db.execute(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT INTO messages
                    (group_id, sender_id, sender_name, role, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (100, 1, "舊成員", "user", "舊訊息", int(time.time())),
            )
            db.execute(
                "CREATE TABLE runtime_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO runtime_settings (key, value) VALUES ('ai_enabled', 'false')"
            )
            db.commit()
            db.close()

            async def scenario() -> None:
                store = MemoryStore(path, ttl_hours=24)
                await store.open()
                migrated = await store.recent_group("primary", 100, 10)
                self.assertEqual([item.content for item in migrated], ["舊訊息"])
                self.assertEqual(
                    await store.legacy_runtime_settings(),
                    {"ai_enabled": "false"},
                )
                await store.close()

                reopened = MemoryStore(path, ttl_hours=24)
                await reopened.open()
                self.assertEqual(
                    len(await reopened.recent_group("primary", 100, 10)),
                    1,
                )
                await reopened.close()

            asyncio.run(scenario())

    def test_account_crud_uses_revision_and_redacts_secrets(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            record = account_record()
            await store.create_account(record)
            loaded = await store.get_account("alpha")
            self.assertIsNotNone(loaded)
            self.assertNotIn("session_ciphertext", loaded.public_dict())

            saved = await store.update_account(
                record.with_updates(ai_model="model-b"),
                expected_revision=1,
                changed_fields=["ai_model"],
            )
            self.assertEqual(saved.revision, 2)
            self.assertEqual((await store.get_account("alpha")).ai_model, "model-b")
            with self.assertRaises(RuntimeError):
                await store.update_account(
                    record.with_updates(ai_model="stale"),
                    expected_revision=1,
                    changed_fields=["ai_model"],
                )
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_legacy_account_api_keys_are_purged_without_decryption(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            await store.create_account(
                account_record().with_updates(
                    ai_api_key_ciphertext="opaque-legacy-ciphertext"
                )
            )

            self.assertEqual(await store.clear_account_api_keys(), 1)
            loaded = await store.get_account("alpha")
            self.assertEqual(loaded.ai_api_key_ciphertext, "")
            self.assertFalse(loaded.has_custom_api_key)
            self.assertEqual(await store.clear_account_api_keys(), 0)
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_account_policy_round_trips_through_create_update_and_public_data(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            record = account_record().with_updates(
                blocked_terms=("邀約", "信用卡"),
                blocked_topics=("政治宣傳", "未成年相關內容"),
                adult_text_enabled=True,
            )
            await store.create_account(record)

            loaded = await store.get_account("alpha")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.blocked_terms, ("邀約", "信用卡"))
            self.assertEqual(
                loaded.blocked_topics,
                ("政治宣傳", "未成年相關內容"),
            )
            self.assertEqual(
                loaded.public_dict()["blocked_terms"],
                ["邀約", "信用卡"],
            )
            self.assertEqual(
                loaded.public_dict()["blocked_topics"],
                ["政治宣傳", "未成年相關內容"],
            )
            self.assertTrue(loaded.adult_text_enabled)
            self.assertTrue(loaded.public_dict()["adult_text_enabled"])

            saved = await store.update_account(
                loaded.with_updates(
                    blocked_terms=("投資群",),
                    blocked_topics=("借貸", "個資交換"),
                    adult_text_enabled=False,
                ),
                expected_revision=loaded.revision,
                changed_fields=[
                    "blocked_terms",
                    "blocked_topics",
                    "adult_text_enabled",
                ],
            )
            self.assertEqual(saved.revision, 2)
            reloaded = await store.get_account("alpha")
            self.assertEqual(reloaded.blocked_terms, ("投資群",))
            self.assertEqual(reloaded.blocked_topics, ("借貸", "個資交換"))
            self.assertFalse(reloaded.adult_text_enabled)
            await store.close()

            db = sqlite3.connect(path)
            raw = db.execute(
                "SELECT blocked_terms, blocked_topics, adult_text_enabled "
                "FROM accounts WHERE id = 'alpha'"
            ).fetchone()
            db.close()
            self.assertEqual(json.loads(raw[0]), ["投資群"])
            self.assertEqual(json.loads(raw[1]), ["借貸", "個資交換"])
            self.assertEqual(raw[2], 0)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_account_media_settings_round_trip_and_default_disabled(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            default_record = account_record()
            await store.create_account(default_record)
            loaded = await store.get_account("alpha")
            self.assertIsNotNone(loaded)
            for feature in loaded.media_settings.public_dict().values():
                self.assertFalse(feature["enabled"])

            configured = AccountMediaSettings(
                image=MediaFeatureSettings(
                    enabled=True,
                    model="gpt-image-1",
                    daily_limit=12,
                    cooldown_seconds=90,
                    allowed_group_ids=frozenset({-1002, -1001}),
                ),
                voice=MediaFeatureSettings(
                    enabled=True,
                    model="azure-speech",
                    voice="zh-TW-HsiaoChenNeural",
                    daily_limit=20,
                    cooldown_seconds=30,
                    allowed_group_ids=frozenset({-1001}),
                ),
            )
            saved = await store.update_account(
                loaded.with_updates(media_settings=configured),
                expected_revision=loaded.revision,
                changed_fields=["media"],
            )
            self.assertEqual(saved.media_settings, configured)
            reloaded = await store.get_account("alpha")
            self.assertEqual(reloaded.media_settings, configured)
            public = reloaded.public_dict()
            self.assertEqual(
                public["media"]["image"]["allowed_group_ids"],
                [-1002, -1001],
            )
            self.assertNotIn("api_key", json.dumps(public["media"]))
            await store.close()

            db = sqlite3.connect(path)
            raw = db.execute(
                "SELECT media_settings FROM accounts WHERE id = 'alpha'"
            ).fetchone()[0]
            db.close()
            self.assertEqual(json.loads(raw)["voice"]["voice"], "zh-TW-HsiaoChenNeural")

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_openrouter_default_migration_is_idempotent(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            legacy_media = AccountMediaSettings(
                image=MediaFeatureSettings(model="gpt-image-1.5"),
                voice=MediaFeatureSettings(
                    model="azure-speech",
                    voice="zh-TW-HsiaoChenNeural",
                ),
                video=MediaFeatureSettings(model="sora-2"),
            )
            await store.create_account(
                account_record().with_updates(
                    gender="female",
                    ai_base_url="https://api.openai.com/v1",
                    ai_model="gpt-5-mini",
                    media_settings=legacy_media,
                )
            )

            migrated = await store.migrate_openrouter_defaults(
                ai_base_url="https://openrouter.ai/api/v1",
                ai_model="x-ai/grok-4.20",
                image_model="x-ai/grok-imagine-image-quality",
                tts_model="x-ai/grok-voice-tts-1.0",
                video_model="x-ai/grok-imagine-video-1.5",
            )
            loaded = await store.get_account("alpha")

            self.assertEqual(migrated, 1)
            self.assertEqual(
                loaded.ai_base_url,
                "https://openrouter.ai/api/v1",
            )
            self.assertEqual(loaded.ai_model, "x-ai/grok-4.20")
            self.assertEqual(
                loaded.media_settings.image.model,
                "x-ai/grok-imagine-image-quality",
            )
            self.assertEqual(
                loaded.media_settings.voice.model,
                "x-ai/grok-voice-tts-1.0",
            )
            self.assertEqual(loaded.media_settings.voice.voice, "eve")
            self.assertEqual(
                loaded.media_settings.video.model,
                "x-ai/grok-imagine-video-1.5",
            )
            self.assertEqual(
                await store.migrate_openrouter_defaults(
                    ai_base_url="https://openrouter.ai/api/v1",
                    ai_model="x-ai/grok-4.20",
                    image_model="x-ai/grok-imagine-image-quality",
                    tts_model="x-ai/grok-voice-tts-1.0",
                    video_model="x-ai/grok-imagine-video-1.5",
                ),
                0,
            )
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_operator_grok_adult_migration_updates_every_existing_account(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            await store.create_account(account_record("alpha", revision=3))
            await store.create_account(
                account_record("beta", revision=8).with_updates(
                    ai_base_url="https://another.example/v1",
                    ai_model="custom-model",
                    adult_text_enabled=False,
                )
            )

            migrated = await store.migrate_existing_accounts_to_grok_adult()
            alpha = await store.get_account("alpha")
            beta = await store.get_account("beta")

            self.assertEqual(migrated, 2)
            self.assertEqual(alpha.ai_base_url, "https://openrouter.ai/api/v1")
            self.assertEqual(beta.ai_base_url, "https://openrouter.ai/api/v1")
            self.assertEqual(alpha.ai_model, "x-ai/grok-4.20")
            self.assertEqual(beta.ai_model, "x-ai/grok-4.20")
            self.assertTrue(alpha.adult_text_enabled)
            self.assertTrue(beta.adult_text_enabled)
            self.assertEqual(alpha.revision, 4)
            self.assertEqual(beta.revision, 9)
            self.assertGreaterEqual(alpha.updated_at, alpha.created_at)
            self.assertGreaterEqual(beta.updated_at, beta.created_at)
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_media_quota_and_queue_are_atomic_and_persistent(self) -> None:
        async def scenario(path: str) -> None:
            first = MemoryStore(path, ttl_hours=24)
            second = MemoryStore(path, ttl_hours=24)
            await first.open()
            await first.create_account(account_record())
            await second.open()
            now = int(time.time())

            initial = await first.reserve_media_quota(
                "alpha",
                "image",
                daily_limit=2,
                cooldown_seconds=0,
                now=now,
            )
            self.assertTrue(initial.allowed)
            concurrent = await asyncio.gather(
                first.reserve_media_quota(
                    "alpha",
                    "image",
                    daily_limit=2,
                    cooldown_seconds=0,
                    now=now + 1,
                ),
                second.reserve_media_quota(
                    "alpha",
                    "image",
                    daily_limit=2,
                    cooldown_seconds=0,
                    now=now + 1,
                ),
            )
            self.assertEqual(sum(item.allowed for item in concurrent), 1)
            denied = next(item for item in concurrent if not item.allowed)
            self.assertEqual(denied.reason, "daily_limit")
            self.assertEqual(denied.remaining, 0)

            voice_first = await first.reserve_media_quota(
                "alpha",
                "voice",
                daily_limit=10,
                cooldown_seconds=60,
                now=now,
            )
            voice_denied = await second.reserve_media_quota(
                "alpha",
                "voice",
                daily_limit=10,
                cooldown_seconds=60,
                now=now + 20,
            )
            voice_next = await first.reserve_media_quota(
                "alpha",
                "voice",
                daily_limit=10,
                cooldown_seconds=60,
                now=now + 60,
            )
            self.assertTrue(voice_first.allowed)
            self.assertFalse(voice_denied.allowed)
            self.assertEqual(voice_denied.reason, "cooldown")
            self.assertEqual(voice_denied.retry_after_seconds, 40)
            self.assertTrue(voice_next.allowed)

            reservation = await first.enqueue_media_job(
                "alpha",
                -1001,
                "video",
                dict(
                    {"prompt": "夜晚城市", "model": "video-model"},
                    source_message_id=101,
                ),
                daily_limit=3,
                cooldown_seconds=30,
                now=now,
            )
            self.assertTrue(reservation.quota.allowed)
            self.assertIsNotNone(reservation.job)
            blocked = await second.enqueue_media_job(
                "alpha",
                -1001,
                "video",
                dict(
                    {"prompt": "第二段影片"},
                    source_message_id=102,
                ),
                daily_limit=3,
                cooldown_seconds=30,
                now=now + 10,
            )
            self.assertFalse(blocked.quota.allowed)
            self.assertIsNone(blocked.job)

            claimed = await second.claim_next_media_job("alpha", now=now)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.status, "running")
            self.assertEqual(claimed.attempts, 1)
            finished = await first.finish_media_job(
                claimed.id,
                "completed",
                result_ref="telegram:file-reference",
                now=now + 5,
            )
            self.assertEqual(finished.status, "completed")
            self.assertEqual(finished.payload, {})
            self.assertIsNone(
                await second.claim_next_media_job("alpha", now=now + 100)
            )
            await second.close()
            await first.close()

            reopened = MemoryStore(path, ttl_hours=24)
            await reopened.open()
            self.assertIsNone(
                await reopened.claim_next_media_job("alpha", now=now + 100)
            )
            await reopened.close()

            db = sqlite3.connect(path)
            job = db.execute(
                "SELECT status, payload FROM media_jobs"
            ).fetchone()
            db.close()
            self.assertEqual(job[0], "completed")
            self.assertEqual(json.loads(job[1]), {})

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_media_jobs_are_account_scoped_and_recoverable(self) -> None:
        async def scenario(path: str) -> None:
            first = MemoryStore(path, ttl_hours=24)
            second = MemoryStore(path, ttl_hours=24)
            await first.open()
            await first.create_account(account_record("alpha"))
            await first.create_account(account_record("beta"))
            await second.open()
            now = int(time.time())

            alpha_reservation = await first.enqueue_media_job(
                "alpha",
                -1001,
                "image",
                {
                    "prompt": "alpha image",
                    "model": "image-model",
                    "source_message_id": 201,
                },
                daily_limit=5,
                cooldown_seconds=0,
                now=now,
            )
            beta_reservation = await second.enqueue_media_job(
                "beta",
                -2001,
                "voice",
                {
                    "prompt": "beta voice",
                    "model": "voice-model",
                    "source_message_id": 202,
                },
                daily_limit=5,
                cooldown_seconds=0,
                now=now,
            )
            self.assertIsNotNone(alpha_reservation.job)
            self.assertIsNotNone(beta_reservation.job)

            claimed_alpha = await second.claim_next_media_job(
                "alpha",
                now=now,
            )
            claimed_beta = await first.claim_next_media_job(
                "beta",
                now=now,
            )
            self.assertEqual(claimed_alpha.account_id, "alpha")
            self.assertEqual(claimed_beta.account_id, "beta")
            self.assertEqual(claimed_alpha.payload["source_message_id"], 201)
            self.assertEqual(claimed_beta.payload["source_message_id"], 202)

            alpha_jobs = await first.list_media_jobs("alpha")
            beta_jobs = await second.list_media_jobs("beta")
            self.assertEqual([job.account_id for job in alpha_jobs], ["alpha"])
            self.assertEqual([job.account_id for job in beta_jobs], ["beta"])
            self.assertEqual(alpha_jobs[0].status, "running")
            self.assertEqual(beta_jobs[0].status, "running")

            self.assertEqual(
                await first.recover_stale_media_jobs("alpha", now=now + 1),
                1,
            )
            self.assertEqual(
                (await second.list_media_jobs("beta"))[0].status,
                "running",
            )
            recovered_alpha = await second.claim_next_media_job(
                "alpha",
                now=now + 1,
            )
            self.assertEqual(recovered_alpha.attempts, 2)
            await first.finish_media_job(
                recovered_alpha.id,
                "completed",
                result_ref="telegram:alpha",
                now=now + 2,
            )

            self.assertEqual(
                await second.recover_stale_media_jobs("beta", now=now + 2),
                1,
            )
            recovered_beta = await first.claim_next_media_job(
                "beta",
                now=now + 2,
            )
            self.assertEqual(recovered_beta.attempts, 2)
            await second.finish_media_job(
                recovered_beta.id,
                "completed",
                result_ref="telegram:beta",
                now=now + 3,
            )
            await second.close()
            await first.close()

            reopened = MemoryStore(path, ttl_hours=24)
            await reopened.open()
            self.assertEqual(
                [job.status for job in await reopened.list_media_jobs("alpha")],
                ["completed"],
            )
            self.assertEqual(
                [job.status for job in await reopened.list_media_jobs("beta")],
                ["completed"],
            )
            await reopened.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_expired_media_jobs_drop_sensitive_payloads_and_history(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            await store.create_account(account_record())
            now = 1_800_000_000
            old = now - 24 * 60 * 60 - 2

            queued = await store.enqueue_media_job(
                "alpha",
                -1001,
                "image",
                {"prompt": "old prompt", "source_message_id": 1},
                daily_limit=10,
                cooldown_seconds=0,
                now=old,
            )
            finished = await store.enqueue_media_job(
                "alpha",
                -1001,
                "voice",
                {"prompt": "old script", "source_message_id": 2},
                daily_limit=10,
                cooldown_seconds=0,
                now=old,
            )
            await store.finish_media_job(
                finished.job.id,
                "completed",
                result_ref="telegram:old",
                now=old + 1,
            )

            self.assertEqual(await store.purge_expired(now=now), 0)
            jobs = await store.list_media_jobs("alpha")
            self.assertEqual([job.id for job in jobs], [queued.job.id])
            self.assertEqual(jobs[0].status, "cancelled")
            self.assertEqual(jobs[0].payload, {})
            self.assertEqual(jobs[0].error, "expired")
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_v2_policy_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "v2.db")

            async def seed() -> None:
                store = MemoryStore(path, ttl_hours=24)
                await store.open()
                await store.create_account(account_record())
                await store.close()

            asyncio.run(seed())
            rebuild_as_v2_account_schema(path)

            async def migrate_twice() -> None:
                for _ in range(2):
                    store = MemoryStore(path, ttl_hours=24)
                    await store.open()
                    loaded = await store.get_account("alpha")
                    self.assertEqual(loaded.blocked_terms, ())
                    self.assertEqual(loaded.blocked_topics, ())
                    self.assertEqual(
                        loaded.media_settings,
                        AccountMediaSettings(),
                    )
                    self.assertFalse(loaded.adult_text_enabled)
                    await store.close()

            asyncio.run(migrate_twice())

            db = sqlite3.connect(path)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(accounts)").fetchall()
            }
            version = db.execute("PRAGMA user_version").fetchone()[0]
            private_alert_targets = {
                foreign_key[2]
                for foreign_key in db.execute(
                    "PRAGMA foreign_key_list(private_alerts)"
                ).fetchall()
            }
            row = db.execute(
                "SELECT blocked_terms, blocked_topics FROM accounts WHERE id = 'alpha'"
            ).fetchone()
            db.close()
            self.assertIn("blocked_terms", columns)
            self.assertIn("blocked_topics", columns)
            self.assertIn("media_settings", columns)
            self.assertIn("adult_text_enabled", columns)
            self.assertEqual(version, 6)
            self.assertEqual(private_alert_targets, {"accounts"})
            self.assertEqual(row, ("[]", "[]"))

    def test_policy_migration_repairs_an_interrupted_single_column_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "interrupted.db")

            async def seed() -> None:
                store = MemoryStore(path, ttl_hours=24)
                await store.open()
                await store.create_account(
                    account_record().with_updates(blocked_terms=("保留規則",))
                )
                await store.close()

            asyncio.run(seed())
            rebuild_as_v2_account_schema(path, keep_blocked_terms=True)

            async def repair() -> None:
                store = MemoryStore(path, ttl_hours=24)
                await store.open()
                loaded = await store.get_account("alpha")
                self.assertEqual(loaded.blocked_terms, ("保留規則",))
                self.assertEqual(loaded.blocked_topics, ())
                await store.close()

                reopened = MemoryStore(path, ttl_hours=24)
                await reopened.open()
                self.assertEqual(
                    (await reopened.get_account("alpha")).blocked_terms,
                    ("保留規則",),
                )
                await reopened.close()

            asyncio.run(repair())

            db = sqlite3.connect(path)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(accounts)").fetchall()
            }
            row = db.execute(
                "SELECT blocked_terms, blocked_topics FROM accounts WHERE id = 'alpha'"
            ).fetchone()
            db.close()
            self.assertIn("blocked_topics", columns)
            self.assertEqual(json.loads(row[0]), ["保留規則"])
            self.assertEqual(json.loads(row[1]), [])

    def test_first_account_adopts_orphaned_legacy_messages(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            await store.add(
                "primary",
                100,
                1,
                "舊成員",
                "user",
                "舊訊息",
            )
            await store.create_account(account_record("alpha"))
            self.assertEqual(await store.adopt_legacy_messages("alpha"), 1)
            self.assertEqual(
                [item.content for item in await store.recent_group("alpha", 100, 10)],
                ["舊訊息"],
            )
            self.assertEqual(await store.recent_group("primary", 100, 10), [])
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


if __name__ == "__main__":
    unittest.main()
