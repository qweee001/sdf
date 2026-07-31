from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import Settings
from app.crypto import SecretBox
from app.manager import AccountManager
from app.memory import MemoryStore
from app.security import safe_error
from app.telegram_login import VerifiedTelegramSession


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
        memory_history_limit=30,
        memory_db_path=path,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password="strong-dashboard-password",
        dashboard_port=8000,
        max_accounts=5,
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


class ManagerTests(unittest.TestCase):
    def test_cancelled_phone_account_creation_releases_login_claim(self) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = settings(path, key)
            store = MemoryStore(path, ttl_hours=24)
            secrets = SecretBox(key)
            entered = asyncio.Event()
            never_finishes = asyncio.Event()

            class FakeLogin:
                released = False

                async def claim_authorized(
                    self,
                    owner_id: str,
                    auth_id: object,
                ) -> VerifiedTelegramSession:
                    return VerifiedTelegramSession(
                        session_ciphertext=secrets.encrypt("phone-login-session"),
                        session_fingerprint=secrets.fingerprint("phone-login-session"),
                        telegram_user_id=112233,
                        telegram_name="取消測試帳號",
                    )

                async def release_claim(self, owner_id: str, auth_id: object) -> None:
                    self.released = True

                async def complete(self, owner_id: str, auth_id: object) -> None:
                    raise AssertionError("cancelled create must not complete")

                async def close(self) -> None:
                    return None

            login = FakeLogin()
            manager = AccountManager(
                config,
                store,
                secrets,
                telegram_login=login,  # type: ignore[arg-type]
            )
            await store.open()

            async def slow_create(
                payload: dict[str, object],
                verified: VerifiedTelegramSession,
            ) -> dict[str, object]:
                entered.set()
                await never_finishes.wait()
                return {}

            manager._create_verified_account = slow_create  # type: ignore[method-assign]
            creating = asyncio.create_task(
                manager.create_account(
                    {
                        "telegram_auth_id": "opaque-flow",
                        "task_name": "自然群聊",
                    },
                    owner_id="dashboard-owner",
                )
            )
            await entered.wait()
            creating.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await creating
            self.assertTrue(login.released)
            await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_phone_authorized_session_is_saved_without_reverification(self) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = settings(path, key)
            store = MemoryStore(path, ttl_hours=24)
            secrets = SecretBox(key)

            class FakeLogin:
                completed = False
                released = False

                async def claim_authorized(
                    self,
                    owner_id: str,
                    auth_id: object,
                ) -> VerifiedTelegramSession:
                    self.assert_values = (owner_id, auth_id)
                    return VerifiedTelegramSession(
                        session_ciphertext=secrets.encrypt("phone-login-session"),
                        session_fingerprint=secrets.fingerprint("phone-login-session"),
                        telegram_user_id=776655,
                        telegram_name="手機登入帳號",
                    )

                async def release_claim(self, owner_id: str, auth_id: object) -> None:
                    self.released = True

                async def complete(self, owner_id: str, auth_id: object) -> None:
                    self.completed = True

                async def close(self) -> None:
                    return None

            login = FakeLogin()
            manager = AccountManager(
                config,
                store,
                secrets,
                telegram_login=login,  # type: ignore[arg-type]
            )
            await store.open()

            async def must_not_verify(session: str) -> tuple[int, str]:
                raise AssertionError("phone login must not reconnect for verification")

            manager.verify_session = must_not_verify  # type: ignore[method-assign]
            created = await manager.create_account(
                {
                    "telegram_auth_id": "opaque-flow",
                    "label": "手機帳號",
                    "enabled": False,
                    "gender": "female",
                    "stage": "observer",
                    "task_name": "自然群聊",
                },
                owner_id="dashboard-owner",
            )
            stored = await store.get_account(str(created["id"]))
            self.assertEqual(stored.telegram_user_id, 776655)
            self.assertEqual(
                secrets.decrypt(stored.session_ciphertext),
                "phone-login-session",
            )
            self.assertTrue(login.completed)
            self.assertFalse(login.released)
            await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_legacy_import_survives_temporary_telegram_failure(self) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = replace(
                settings(path, key),
                legacy_session_string="legacy-session-secret",
            )
            store = MemoryStore(path, ttl_hours=24)
            secrets = SecretBox(key)
            manager = AccountManager(config, store, secrets)
            await store.open()

            async def unavailable(session: str) -> tuple[int, str]:
                self.assertEqual(session, "legacy-session-secret")
                raise OSError("temporary Telegram connection failure")

            manager.verify_session = unavailable  # type: ignore[method-assign]
            await manager._import_legacy_account()
            imported = await store.get_account("primary")
            self.assertIsNotNone(imported)
            self.assertLess(imported.telegram_user_id, 0)
            self.assertEqual(
                secrets.decrypt(imported.session_ciphertext),
                "legacy-session-secret",
            )
            await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_create_and_update_account_keeps_credentials_secret(self) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = settings(path, key)
            store = MemoryStore(path, ttl_hours=24)
            secrets = SecretBox(key)
            manager = AccountManager(config, store, secrets)
            await store.open()

            async def fake_verify(session: str) -> tuple[int, str]:
                self.assertEqual(session, "session-secret")
                return 9988, "測試 Telegram"

            manager.verify_session = fake_verify  # type: ignore[method-assign]
            created = await manager.create_account(
                {
                    "label": "帳號 A",
                    "session_string": "session-secret",
                    "enabled": False,
                    "gender": "female",
                    "stage": "observer",
                    "task_name": "歡迎新人",
                    "task_info": "自然歡迎新加入的成員",
                    "ai_base_url": "https://api.example.com/v1",
                    "ai_model": "model-a",
                }
            )
            account_id = str(created["id"])
            self.assertNotIn("session_string", created)
            self.assertNotIn("session_ciphertext", created)
            stored = await store.get_account(account_id)
            self.assertEqual(secrets.decrypt(stored.session_ciphertext), "session-secret")

            updated = await manager.update_account(
                account_id,
                {
                    "revision": created["revision"],
                    "label": "帳號 A2",
                    "gender": "female",
                    "stage": "observer",
                    "task_name": "新任務",
                    "task_info": "更新後任務",
                    "ai_base_url": "https://api.example.com/v2",
                    "ai_model": "model-b",
                    "ai_api_key": "custom-secret",
                },
            )
            self.assertEqual(updated["ai_model"], "model-b")
            self.assertTrue(updated["has_custom_api_key"])
            self.assertNotIn("ai_api_key", updated)
            await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_blocked_policy_is_normalized_deduplicated_and_public(self) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = settings(path, key)
            store = MemoryStore(path, ttl_hours=24)
            secrets = SecretBox(key)
            manager = AccountManager(config, store, secrets)
            await store.open()

            async def fake_verify(session: str) -> tuple[int, str]:
                self.assertEqual(session, "policy-session")
                return 445566, "政策測試帳號"

            manager.verify_session = fake_verify  # type: ignore[method-assign]
            created = await manager.create_account(
                {
                    "session_string": "policy-session",
                    "enabled": False,
                    "gender": "female",
                    "stage": "observer",
                    "task_name": "一般群聊",
                    "blocked_terms": [
                        "  ＴＥＳＴ  ",
                        "test",
                        "  咖   啡  ",
                        "咖 啡",
                        "",
                    ],
                    "blocked_topics": [
                        "  私密   話題  ",
                        "私密 話題",
                        "安全",
                    ],
                }
            )
            self.assertEqual(created["blocked_terms"], ["TEST", "咖 啡"])
            self.assertEqual(created["blocked_topics"], ["私密 話題", "安全"])
            self.assertNotIn("session_string", created)
            self.assertNotIn("session_ciphertext", created)

            account_id = str(created["id"])
            stored = await store.get_account(account_id)
            self.assertEqual(stored.blocked_terms, ("TEST", "咖 啡"))
            self.assertEqual(stored.blocked_topics, ("私密 話題", "安全"))
            self.assertEqual(
                stored.public_dict()["blocked_terms"],
                ["TEST", "咖 啡"],
            )

            updated = await manager.update_account(
                account_id,
                {
                    "revision": created["revision"],
                    "blocked_terms": ["  ＡＢＣ  ", "abc"],
                    "blocked_topics": ["  借貸   推廣 ", "借貸 推廣"],
                },
            )
            self.assertEqual(updated["blocked_terms"], ["ABC"])
            self.assertEqual(updated["blocked_topics"], ["借貸 推廣"])
            reloaded = await store.get_account(account_id)
            self.assertEqual(reloaded.blocked_terms, ("ABC",))
            self.assertEqual(reloaded.blocked_topics, ("借貸 推廣",))
            await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_blocked_policy_manager_limits_are_enforced(self) -> None:
        async def scenario(path: str) -> None:
            key = Fernet.generate_key().decode()
            config = settings(path, key)
            store = MemoryStore(path, ttl_hours=24)
            secrets = SecretBox(key)
            manager = AccountManager(config, store, secrets)
            await store.open()

            async def fake_verify(session: str) -> tuple[int, str]:
                return 778899, "限制測試帳號"

            manager.verify_session = fake_verify  # type: ignore[method-assign]
            base_payload: dict[str, object] = {
                "session_string": "limit-session",
                "enabled": False,
                "gender": "male",
                "stage": "observer",
                "task_name": "限制測試",
            }
            cases = (
                (
                    "blocked_terms",
                    [f"term-{index}" for index in range(101)],
                    "at most 100 items",
                ),
                (
                    "blocked_topics",
                    [f"topic-{index}" for index in range(51)],
                    "at most 50 items",
                ),
                (
                    "blocked_terms",
                    ["x" * 81],
                    "at most 80 characters",
                ),
                (
                    "blocked_topics",
                    ["x" * 301],
                    "at most 300 characters",
                ),
                (
                    "blocked_terms",
                    [f"{index:02d}" + "x" * 78 for index in range(51)],
                    "total length must be at most 4000",
                ),
                (
                    "blocked_topics",
                    [f"{index:02d}" + "x" * 298 for index in range(27)],
                    "total length must be at most 8000",
                ),
            )
            for field, value, message in cases:
                with self.subTest(field=field, message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        await manager.create_account(
                            {
                                **base_payload,
                                field: value,
                            }
                        )

            self.assertEqual(await store.count_accounts(), 0)
            await manager.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_provider_url_rejects_private_targets(self) -> None:
        from app.account import validate_provider_url

        for url in (
            "http://api.example.com/v1",
            "https://localhost/v1",
            "https://metadata.google.internal/v1",
            "https://127.0.0.1/v1",
            "https://169.254.169.254/latest",
            "https://api.example.com/v1?key=secret",
        ):
            with self.assertRaises(ValueError):
                validate_provider_url(url)
        self.assertEqual(
            validate_provider_url("https://api.example.com/v1/"),
            "https://api.example.com/v1",
        )

    def test_provider_errors_are_redacted(self) -> None:
        message = safe_error(
            RuntimeError(
                "Authorization: Bearer sk-very-secret-token "
                "api_key=another-secret"
            )
        )
        self.assertNotIn("very-secret", message)
        self.assertNotIn("another-secret", message)
        self.assertIn("[redacted]", message)


if __name__ == "__main__":
    unittest.main()
