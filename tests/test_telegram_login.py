from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from cryptography.fernet import Fernet
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from app.crypto import SecretBox
from app.telegram_login import (
    TelegramLoginConflict,
    TelegramLoginExpired,
    TelegramLoginService,
    TelegramLoginUnavailable,
)


class FakeStringSession:
    def __init__(self, value: str) -> None:
        self.value = value

    def save(self) -> str:
        return self.value


class FakeTelegramClient:
    def __init__(self, scenario: str = "normal") -> None:
        self.scenario = scenario
        self.connected = False
        self.disconnect_count = 0
        self.sent_phone = ""
        self.code_calls: list[tuple[str, str, str]] = []
        self.password_calls: list[str] = []
        self.session = FakeStringSession("1" + "A" * 100)

    async def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1

    async def send_code_request(self, phone: str) -> object:
        self.sent_phone = phone
        return SimpleNamespace(phone_code_hash="server-only-phone-code-hash")

    async def sign_in(self, **kwargs: str) -> None:
        if "password" in kwargs:
            password = kwargs["password"]
            self.password_calls.append(password)
            if self.scenario == "2fa" and password == "wrong-password":
                raise PasswordHashInvalidError(None)
            return
        self.code_calls.append(
            (
                kwargs["phone"],
                kwargs["code"],
                kwargs["phone_code_hash"],
            )
        )
        if self.scenario == "2fa":
            raise SessionPasswordNeededError(None)
        if self.scenario == "invalid_code":
            raise PhoneCodeInvalidError(None)

    async def get_me(self) -> object:
        return SimpleNamespace(id=998877, first_name="測試", last_name="帳號")


class ClientFactory:
    def __init__(self, *clients: FakeTelegramClient) -> None:
        self.clients = list(clients)

    def __call__(self, *args: object, **kwargs: object) -> FakeTelegramClient:
        return self.clients.pop(0)


class TelegramLoginTests(unittest.TestCase):
    def make_service(
        self,
        client: FakeTelegramClient,
        now: list[float] | None = None,
    ) -> tuple[TelegramLoginService, SecretBox]:
        secret_box = SecretBox(Fernet.generate_key().decode())
        clock = (lambda: now[0]) if now is not None else None
        kwargs = {
            "client_factory": ClientFactory(client),
        }
        if clock is not None:
            kwargs["clock"] = clock
        service = TelegramLoginService(
            12345,
            "hash",
            secret_box,
            **kwargs,  # type: ignore[arg-type]
        )
        return service, secret_box

    def test_code_login_encrypts_session_and_never_returns_secrets(self) -> None:
        async def scenario() -> None:
            client = FakeTelegramClient()
            service, secret_box = self.make_service(client)
            started = await service.start("dashboard-session", "+886912345678")
            self.assertEqual(started["status"], "code_required")
            self.assertNotIn("+886912345678", str(started))
            self.assertNotIn("phone-code-hash", str(started))

            authorized = await service.submit_code(
                "dashboard-session",
                started["auth_id"],
                "01234",
            )
            self.assertEqual(authorized["status"], "authorized")
            self.assertNotIn("01234", str(authorized))
            self.assertFalse(client.is_connected())

            verified = await service.claim_authorized(
                "dashboard-session",
                started["auth_id"],
            )
            self.assertEqual(verified.telegram_user_id, 998877)
            self.assertEqual(
                secret_box.decrypt(verified.session_ciphertext),
                "1" + "A" * 100,
            )
            self.assertNotIn("A" * 20, str(authorized))
            await service.complete("dashboard-session", started["auth_id"])
            with self.assertRaises(TelegramLoginExpired):
                await service.claim_authorized(
                    "dashboard-session",
                    started["auth_id"],
                )
            await service.close()

        asyncio.run(scenario())

    def test_two_factor_flow_retries_password_without_persisting_it(self) -> None:
        async def scenario() -> None:
            client = FakeTelegramClient("2fa")
            service, _ = self.make_service(client)
            started = await service.start("owner", "+886923456789")
            needs_password = await service.submit_code(
                "owner",
                started["auth_id"],
                "12345",
            )
            self.assertEqual(needs_password["status"], "password_required")

            with self.assertRaisesRegex(ValueError, "不正確"):
                await service.submit_password(
                    "owner",
                    started["auth_id"],
                    "wrong-password",
                )
            authorized = await service.submit_password(
                "owner",
                started["auth_id"],
                "correct-password",
            )
            self.assertEqual(authorized["status"], "authorized")
            pending = service.pending[str(started["auth_id"])]
            self.assertFalse(hasattr(pending, "password"))
            self.assertEqual(pending.phone, "")
            self.assertEqual(pending.phone_code_hash, "")
            await service.close()

        asyncio.run(scenario())

    def test_invalid_code_limit_expiry_and_owner_binding(self) -> None:
        async def scenario() -> None:
            now = [1000.0]
            client = FakeTelegramClient("invalid_code")
            service, _ = self.make_service(client, now)
            started = await service.start("owner-a", "+886934567890")

            with self.assertRaises(TelegramLoginExpired):
                await service.submit_code(
                    "owner-b",
                    started["auth_id"],
                    "12345",
                )
            for _ in range(4):
                with self.assertRaisesRegex(ValueError, "不正確"):
                    await service.submit_code(
                        "owner-a",
                        started["auth_id"],
                        "12345",
                    )
            with self.assertRaises(TelegramLoginExpired):
                await service.submit_code(
                    "owner-a",
                    started["auth_id"],
                    "12345",
                )
            self.assertNotIn(started["auth_id"], service.pending)
            self.assertFalse(client.is_connected())

            second = FakeTelegramClient()
            service.client_factory = ClientFactory(second)
            now[0] += 61
            restarted = await service.start("owner-a", "+886934567890")
            now[0] += 601
            with self.assertRaises(TelegramLoginExpired):
                await service.submit_code(
                    "owner-a",
                    restarted["auth_id"],
                    "12345",
                )
            self.assertFalse(second.is_connected())
            await service.close()

        asyncio.run(scenario())

    def test_expired_flow_is_pruned_without_another_login(self) -> None:
        async def scenario() -> None:
            now = [1000.0]
            client = FakeTelegramClient()
            service, _ = self.make_service(client, now)
            started = await service.start("owner", "+886945678901")
            now[0] += 601

            self.assertEqual(await service.prune_expired(), 1)
            self.assertNotIn(started["auth_id"], service.pending)
            self.assertFalse(client.is_connected())
            self.assertEqual(service.last_start, {})
            self.assertEqual(service.last_phone_start, {})
            await service.close()

        asyncio.run(scenario())

    def test_claimed_session_cannot_be_cancelled_mid_create(self) -> None:
        async def scenario() -> None:
            client = FakeTelegramClient()
            service, _ = self.make_service(client)
            started = await service.start("owner", "+886956789012")
            await service.submit_code("owner", started["auth_id"], "12345")
            await service.claim_authorized("owner", started["auth_id"])

            with self.assertRaises(TelegramLoginConflict):
                await service.cancel("owner", started["auth_id"])
            self.assertEqual(await service.cancel_owner("owner"), 0)
            self.assertIn(started["auth_id"], service.pending)

            await service.release_claim("owner", started["auth_id"])
            await service.cancel("owner", started["auth_id"])
            self.assertNotIn(started["auth_id"], service.pending)
            await service.close()

        asyncio.run(scenario())

    def test_expiry_is_rechecked_after_waiting_for_flow_lock(self) -> None:
        async def scenario() -> None:
            now = [1000.0]
            client = FakeTelegramClient()
            service, _ = self.make_service(client, now)
            started = await service.start("owner", "+886967890123")
            pending = service.pending[str(started["auth_id"])]
            await pending.lock.acquire()
            submit = asyncio.create_task(
                service.submit_code("owner", started["auth_id"], "12345")
            )
            await asyncio.sleep(0)
            now[0] += 601
            pending.lock.release()

            with self.assertRaises(TelegramLoginExpired):
                await submit
            self.assertFalse(client.is_connected())
            await service.close()

        asyncio.run(scenario())

    def test_cancel_during_send_code_does_not_leave_orphaned_flow(self) -> None:
        async def scenario() -> None:
            entered = asyncio.Event()
            release = asyncio.Event()

            class SlowClient(FakeTelegramClient):
                async def send_code_request(self, phone: str) -> object:
                    self.sent_phone = phone
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(phone_code_hash="temporary-hash")

            client = SlowClient()
            service, _ = self.make_service(client)
            starting = asyncio.create_task(
                service.start("owner", "+886978901234")
            )
            await entered.wait()
            await service.cancel_owner("owner")
            release.set()

            with self.assertRaises(TelegramLoginExpired):
                await starting
            self.assertEqual(service.pending, {})
            self.assertFalse(client.is_connected())
            await service.close()

        asyncio.run(scenario())

    def test_authorization_finish_error_is_redacted_and_flow_is_closed(self) -> None:
        async def scenario() -> None:
            class BrokenClient(FakeTelegramClient):
                async def get_me(self) -> object:
                    raise RuntimeError(
                        "secret-session +886900000000 server-only-phone-code-hash"
                    )

            client = BrokenClient()
            service, _ = self.make_service(client)
            started = await service.start("owner", "+886989012345")
            with self.assertRaises(TelegramLoginUnavailable) as caught:
                await service.submit_code(
                    "owner",
                    started["auth_id"],
                    "12345",
                )
            rendered = str(caught.exception)
            self.assertNotIn("+886989012345", rendered)
            self.assertNotIn("server-only-phone-code-hash", rendered)
            self.assertNotIn(started["auth_id"], service.pending)
            self.assertFalse(client.is_connected())
            await service.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
