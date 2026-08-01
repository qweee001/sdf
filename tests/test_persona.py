from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.config import Settings
from app.crypto import SecretBox
from app.manager import AccountManager
from app.memory import MemoryStore
from app.persona import generate_account_profile, generate_persona


PROFILE_FIELDS = {
    "label",
    "gender",
    "stage",
    "style",
    "task_name",
    "task_info",
    "ai_model",
    "adult_text_enabled",
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
}


def settings(path: str, key: str) -> Settings:
    return Settings(
        tg_api_id=12345,
        tg_api_hash="hash",
        legacy_session_string="",
        ai_api_key="global-key",
        ai_base_url="https://api.example.com/v1",
        ai_model="default-model",
        account_encryption_key=key,
        memory_ttl_hours=24,
        memory_history_limit=20,
        memory_db_path=path,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password="strong-dashboard-password",
        dashboard_port=8000,
        max_accounts=20,
        log_level="INFO",
        legacy_gender="male",
        legacy_stage="old_member",
        legacy_style="",
        legacy_group_ids=frozenset(),
        legacy_ignore_sender_ids=frozenset(),
        legacy_group_reply_probability=0.35,
        legacy_reply_on_mention=True,
        legacy_reply_on_reply=True,
        legacy_typing_delay_min_seconds=1.5,
        legacy_typing_delay_max_seconds=5,
        legacy_proactive_enabled=True,
        legacy_proactive_idle_minutes=15,
        legacy_proactive_min_interval_minutes=25,
        legacy_proactive_max_interval_minutes=60,
        legacy_max_proactive_per_day=24,
    )


class PersonaGeneratorTests(unittest.TestCase):
    def assert_taiwan_locale_style(self, style: object) -> None:
        rendered = str(style)
        self.assertIn("台灣繁體中文", rendered)
        self.assertIn("台灣常用詞", rendered)
        self.assertIn("簡體", rendered)
        self.assertIn("中國大陸用詞", rendered)
        self.assertIn("翻譯腔", rendered)

    def test_every_persona_contains_the_taiwan_local_language_contract(
        self,
    ) -> None:
        for gender in ("male", "female"):
            for stage in ("old_member", "observer"):
                for adult_text_enabled in (False, True):
                    with self.subTest(
                        gender=gender,
                        stage=stage,
                        adult_text_enabled=adult_text_enabled,
                    ):
                        self.assert_taiwan_locale_style(
                            generate_persona(
                                gender,
                                stage,
                                adult_text_enabled,
                            )
                        )

    def test_every_random_account_profile_keeps_the_locale_contract(self) -> None:
        excluded: list[str] = []
        for _ in range(12):
            profile = generate_account_profile(exclude_style=excluded)
            self.assert_taiwan_locale_style(profile["style"])
            excluded.append(str(profile["style"]))

    def test_full_account_profile_has_only_editable_randomized_fields(self) -> None:
        profile = generate_account_profile()

        self.assertEqual(set(profile), PROFILE_FIELDS)
        self.assertTrue(str(profile["label"]).strip())
        self.assertIn(profile["gender"], {"male", "female"})
        self.assertIn(profile["stage"], {"old_member", "observer"})
        self.assertTrue(str(profile["style"]).strip())
        self.assertTrue(str(profile["task_name"]).strip())
        self.assertTrue(str(profile["task_info"]).strip())
        self.assertTrue(str(profile["ai_model"]).strip())
        self.assertIsInstance(profile["adult_text_enabled"], bool)
        self.assertGreaterEqual(float(profile["group_reply_probability"]), 0)
        self.assertLessEqual(float(profile["group_reply_probability"]), 1)
        self.assertIsInstance(profile["reply_on_mention"], bool)
        self.assertIsInstance(profile["reply_on_reply"], bool)
        self.assertGreaterEqual(float(profile["typing_delay_min_seconds"]), 0)
        self.assertLessEqual(
            float(profile["typing_delay_min_seconds"]),
            float(profile["typing_delay_max_seconds"]),
        )
        self.assertLessEqual(float(profile["typing_delay_max_seconds"]), 60)
        self.assertIsInstance(profile["proactive_enabled"], bool)
        self.assertGreaterEqual(int(profile["proactive_idle_minutes"]), 1)
        self.assertGreaterEqual(int(profile["proactive_min_interval_minutes"]), 1)
        self.assertLessEqual(
            int(profile["proactive_min_interval_minutes"]),
            int(profile["proactive_max_interval_minutes"]),
        )
        self.assertLessEqual(int(profile["proactive_max_interval_minutes"]), 1440)
        self.assertGreaterEqual(int(profile["max_proactive_per_day"]), 0)
        self.assertLessEqual(int(profile["max_proactive_per_day"]), 200)
        if profile["adult_text_enabled"]:
            for required in ("成人純文字", "成年", "自願", "拒絕即停止"):
                self.assertIn(required, str(profile["style"]))

    def test_full_profile_can_exclude_the_current_account_style(self) -> None:
        first = generate_account_profile()
        second = generate_account_profile(exclude_style=(str(first["style"]),))

        self.assertNotEqual(first["style"], second["style"])

    def test_excluding_current_persona_produces_a_different_persona(self) -> None:
        first = generate_persona("female", "old_member", False)
        second = generate_persona(
            "female",
            "old_member",
            False,
            exclude=first,
        )

        self.assertTrue(first.strip())
        self.assertTrue(second.strip())
        self.assertNotEqual(first, second)

    def test_adult_persona_keeps_consent_safety_floor(self) -> None:
        persona = generate_persona("male", "observer", True)

        self.assertIn("成人純文字", persona)
        self.assertIn("成年", persona)
        self.assertIn("自願", persona)
        self.assertIn("拒絕即停止", persona)

    def test_non_adult_persona_does_not_enable_adult_or_explicit_chat(self) -> None:
        persona = generate_persona("male", "observer", False)

        self.assertNotIn("成人純文字", persona)
        self.assertNotIn("色情", persona)
        self.assertNotIn("能自然談曖昧與露骨", persona)
        self.assertIn("不主動帶入露骨成人內容", persona)


class AccountPersonaLifecycleTests(unittest.TestCase):
    def test_start_account_waits_for_the_account_operation_lock(self) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = settings(path, key)
            store = MemoryStore(path, ttl_hours=24)
            manager = AccountManager(config, store, SecretBox(key))
            await store.open()

            async def fake_verify(session: str) -> tuple[int, str]:
                return 80001, "啟動鎖測試帳號"

            manager.verify_session = fake_verify  # type: ignore[method-assign]
            try:
                created = await manager.create_account(
                    {
                        "session_string": "start-lock-session",
                        "style": "原始性格",
                    }
                )
                account_id = str(created["id"])
                observed_revisions: list[int] = []

                async def record_locked_start(target_id: str) -> None:
                    current = await store.get_account(target_id)
                    self.assertIsNotNone(current)
                    observed_revisions.append(int(current.revision))

                manager._start_account_locked = (  # type: ignore[method-assign]
                    record_locked_start
                )
                operation_lock = manager._account_operation_locks[account_id]
                await operation_lock.acquire()
                start_task = asyncio.create_task(manager.start_account(account_id))
                try:
                    await asyncio.sleep(0)
                    self.assertFalse(start_task.done())
                    self.assertEqual(observed_revisions, [])

                    current = await store.get_account(account_id)
                    self.assertIsNotNone(current)
                    saved = await store.update_account(
                        current.with_updates(style="上鎖期間的新性格"),
                        expected_revision=current.revision,
                        changed_fields=["style"],
                    )
                finally:
                    operation_lock.release()

                await asyncio.wait_for(start_task, timeout=2)
                self.assertEqual(observed_revisions, [saved.revision])
            finally:
                await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_concurrent_persona_mutations_cannot_choose_the_same_style(
        self,
    ) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = settings(path, key)
            store = MemoryStore(path, ttl_hours=24)
            manager = AccountManager(config, store, SecretBox(key))
            await store.open()
            identities = {
                "persona-lock-alpha": (81001, "性格鎖 Alpha"),
                "persona-lock-beta": (81002, "性格鎖 Beta"),
            }

            async def fake_verify(session: str) -> tuple[int, str]:
                return identities[session]

            manager.verify_session = fake_verify  # type: ignore[method-assign]
            try:
                alpha = await manager.create_account(
                    {
                        "session_string": "persona-lock-alpha",
                        "style": "Alpha 原始性格",
                    }
                )
                beta = await manager.create_account(
                    {
                        "session_string": "persona-lock-beta",
                        "style": "Beta 原始性格",
                    }
                )
                original_update = store.update_account
                first_update_started = asyncio.Event()
                release_first_update = asyncio.Event()
                update_calls = 0

                async def delayed_update(*args: object, **kwargs: object):
                    nonlocal update_calls
                    update_calls += 1
                    if update_calls == 1:
                        first_update_started.set()
                        await release_first_update.wait()
                    return await original_update(*args, **kwargs)

                def deterministic_persona(
                    gender: str,
                    stage: str,
                    adult_text_enabled: bool,
                    *,
                    exclude: object = "",
                ) -> str:
                    excluded = set(exclude) if not isinstance(exclude, str) else {exclude}
                    return (
                        "共用鎖生成性格 B"
                        if "共用鎖生成性格 A" in excluded
                        else "共用鎖生成性格 A"
                    )

                store.update_account = delayed_update  # type: ignore[method-assign]
                with patch("app.manager.generate_persona", deterministic_persona):
                    first = asyncio.create_task(
                        manager.regenerate_persona(
                            str(alpha["id"]),
                            int(alpha["revision"]),
                        )
                    )
                    await asyncio.wait_for(first_update_started.wait(), timeout=2)
                    second = asyncio.create_task(
                        manager.regenerate_persona(
                            str(beta["id"]),
                            int(beta["revision"]),
                        )
                    )
                    await asyncio.sleep(0)
                    self.assertFalse(second.done())
                    release_first_update.set()
                    first_result, second_result = await asyncio.wait_for(
                        asyncio.gather(first, second),
                        timeout=2,
                    )

                self.assertNotEqual(
                    first_result["style"],
                    second_result["style"],
                )
                self.assertEqual(
                    {first_result["style"], second_result["style"]},
                    {"共用鎖生成性格 A", "共用鎖生成性格 B"},
                )
            finally:
                await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_enabled_clear_memory_restarts_without_reacquiring_its_lock(
        self,
    ) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = settings(path, key)
            store = MemoryStore(path, ttl_hours=24)
            manager = AccountManager(config, store, SecretBox(key))
            await store.open()

            async def fake_verify(session: str) -> tuple[int, str]:
                return 82001, "清除後啟動測試帳號"

            manager.verify_session = fake_verify  # type: ignore[method-assign]
            try:
                created = await manager.create_account(
                    {
                        "session_string": "enabled-clear-session",
                        "style": "清除前性格",
                    }
                )
                account_id = str(created["id"])
                current = await store.get_account(account_id)
                self.assertIsNotNone(current)
                enabled = await store.update_account(
                    current.with_updates(enabled=True),
                    expected_revision=current.revision,
                    changed_fields=["enabled"],
                )
                started_revisions: list[int] = []

                async def record_locked_start(target_id: str) -> None:
                    latest = await store.get_account(target_id)
                    self.assertIsNotNone(latest)
                    started_revisions.append(int(latest.revision))

                manager._start_account_locked = (  # type: ignore[method-assign]
                    record_locked_start
                )
                result = await asyncio.wait_for(
                    manager.clear_memory(account_id, enabled.revision),
                    timeout=2,
                )

                self.assertEqual(
                    started_revisions,
                    [int(result["account"]["revision"])],
                )
                self.assertNotEqual(
                    result["account"]["style"],
                    "清除前性格",
                )
            finally:
                await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_random_profile_preview_does_not_persist_or_restart_account(self) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = settings(path, key)
            store = MemoryStore(path, ttl_hours=24)
            manager = AccountManager(config, store, SecretBox(key))
            await store.open()

            async def fake_verify(session: str) -> tuple[int, str]:
                return 90001, "預覽測試帳號"

            manager.verify_session = fake_verify  # type: ignore[method-assign]
            try:
                created = await manager.create_account(
                    {
                        "session_string": "preview-session",
                        "label": "原始顯示名稱",
                        "gender": "female",
                        "stage": "old_member",
                        "style": "原始手動性格",
                        "task_name": "原始任務",
                        "task_info": "原始任務資訊",
                        "ai_base_url": "https://api.example.com/v1",
                        "ai_model": "original-model",
                        "adult_text_enabled": False,
                        "all_groups": False,
                        "group_ids": [-100111],
                        "blocked_terms": ["保留的屏蔽詞"],
                        "blocked_topics": ["保留的屏蔽主題"],
                    }
                )
                account_id = str(created["id"])
                before = await store.get_account(account_id)
                self.assertIsNotNone(before)

                preview = await manager.preview_random_profile(
                    account_id,
                    int(created["revision"]),
                )

                self.assertEqual(set(preview), PROFILE_FIELDS)
                self.assertNotEqual(preview["style"], before.style)
                forbidden = {
                    "id",
                    "revision",
                    "ai_base_url",
                    "ai_api_key",
                    "session_string",
                    "all_groups",
                    "group_ids",
                    "blocked_terms",
                    "blocked_topics",
                    "media",
                }
                self.assertTrue(forbidden.isdisjoint(preview))
                after = await store.get_account(account_id)
                self.assertEqual(after, before)
                self.assertEqual(after.revision, created["revision"])
                self.assertNotIn(account_id, manager.workers)
                self.assertNotIn(account_id, manager.worker_tasks)
                if preview["adult_text_enabled"]:
                    for required in (
                        "成人純文字",
                        "成年",
                        "自願",
                        "拒絕即停止",
                    ):
                        self.assertIn(required, str(preview["style"]))
            finally:
                await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_blank_styles_are_generated_per_account_and_custom_style_is_kept(
        self,
    ) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = settings(path, key)
            store = MemoryStore(path, ttl_hours=24)
            manager = AccountManager(config, store, SecretBox(key))
            await store.open()
            identities = {
                "session-alpha": (10001, "帳號 Alpha"),
                "session-beta": (10002, "帳號 Beta"),
                "session-custom": (10003, "帳號 Custom"),
            }

            async def fake_verify(session: str) -> tuple[int, str]:
                return identities[session]

            manager.verify_session = fake_verify  # type: ignore[method-assign]
            try:
                alpha = await manager.create_account(
                    {
                        "session_string": "session-alpha",
                        "gender": "female",
                        "stage": "old_member",
                    }
                )
                beta = await manager.create_account(
                    {
                        "session_string": "session-beta",
                        "gender": "female",
                        "stage": "old_member",
                    }
                )
                custom = await manager.create_account(
                    {
                        "session_string": "session-custom",
                        "gender": "female",
                        "stage": "old_member",
                        "style": "管理員手動設定的冷靜幽默性格",
                    }
                )

                self.assertTrue(str(alpha["style"]).strip())
                self.assertTrue(str(beta["style"]).strip())
                self.assertNotEqual(alpha["style"], beta["style"])
                self.assertEqual(
                    custom["style"],
                    "管理員手動設定的冷靜幽默性格",
                )
            finally:
                await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_regenerate_and_clear_memory_each_replace_the_account_persona(
        self,
    ) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = settings(path, key)
            store = MemoryStore(path, ttl_hours=24)
            manager = AccountManager(config, store, SecretBox(key))
            await store.open()

            async def fake_verify(session: str) -> tuple[int, str]:
                return 20001, "成人角色測試帳號"

            manager.verify_session = fake_verify  # type: ignore[method-assign]
            try:
                created = await manager.create_account(
                    {
                        "session_string": "adult-session",
                        "gender": "male",
                        "stage": "old_member",
                        "adult_text_enabled": True,
                    }
                )
                account_id = str(created["id"])
                original_style = str(created["style"])

                regenerated = await manager.regenerate_persona(
                    account_id,
                    int(created["revision"]),
                )
                regenerated_style = str(regenerated["style"])
                self.assertNotEqual(original_style, regenerated_style)
                self.assertGreater(
                    int(regenerated["revision"]),
                    int(created["revision"]),
                )
                for required in ("成年", "自願", "拒絕即停止"):
                    self.assertIn(required, regenerated_style)

                await store.add(
                    account_id,
                    -100111,
                    30001,
                    "群組成員",
                    "user",
                    "這是一則應被清除的記憶",
                    created_at=int(time.time()),
                )
                cleared = await manager.clear_memory(
                    account_id,
                    int(regenerated["revision"]),
                )
                self.assertEqual(cleared["removed"], 1)
                cleared_account = cleared["account"]
                cleared_style = str(cleared_account["style"])
                self.assertNotEqual(regenerated_style, cleared_style)
                self.assertGreater(
                    int(cleared_account["revision"]),
                    int(regenerated["revision"]),
                )
                self.assertEqual(
                    await store.recent_group(account_id, -100111, 20),
                    [],
                )
                for required in ("成年", "自願", "拒絕即停止"):
                    self.assertIn(required, cleared_style)

                stored = await store.get_account(account_id)
                self.assertIsNotNone(stored)
                self.assertEqual(stored.style, cleared_style)
            finally:
                await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


if __name__ == "__main__":
    unittest.main()
