from __future__ import annotations

import unittest
from typing import Any

from fastapi.testclient import TestClient

from app.dashboard import DashboardServer


class FakeAccountManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.account: dict[str, object] = {
            "id": "acct_one",
            "label": "測試帳號",
            "telegram_user_id": 123456,
            "telegram_name": "tester",
            "enabled": True,
            "gender": "female",
            "stage": "observer",
            "task_name": "自然參與群聊",
            "task_info": "回覆群內的一般交友話題",
            "ai_base_url": "https://api.openai.com/v1",
            "ai_model": "gpt-5-mini",
            "has_custom_api_key": True,
            "session_configured": True,
            "all_groups": True,
            "group_ids": [],
            "revision": 1,
            "state": "connected",
        }

    async def status(self) -> dict[str, object]:
        return {
            "ok": True,
            "account_count": 1,
            "memory_ttl_hours": 24,
            "accounts": [dict(self.account)],
        }

    async def create_account(self, payload: dict[str, Any]) -> dict[str, object]:
        self.calls.append(("create", dict(payload)))
        self.account.update(
            {
                "label": payload["label"],
                "task_name": payload["task_name"],
                "ai_model": payload["ai_model"],
                "revision": 1,
            }
        )
        return dict(self.account)

    async def update_account(
        self,
        account_id: str,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        self.calls.append(("update", (account_id, dict(payload))))
        self.account.update(
            {
                "label": payload["label"],
                "task_name": payload["task_name"],
                "task_info": payload["task_info"],
                "ai_model": payload["ai_model"],
                "revision": int(payload["revision"]) + 1,
            }
        )
        return dict(self.account)

    async def set_enabled(
        self,
        account_id: str,
        enabled: bool,
        revision: int,
    ) -> dict[str, object]:
        self.calls.append(("control", (account_id, enabled, revision)))
        self.account["enabled"] = enabled
        self.account["revision"] = revision + 1
        return dict(self.account)

    async def set_groups(
        self,
        account_id: str,
        all_groups: bool,
        group_ids: list[object],
        revision: int,
    ) -> dict[str, object]:
        self.calls.append(
            ("groups", (account_id, all_groups, list(group_ids), revision))
        )
        self.account["all_groups"] = all_groups
        self.account["group_ids"] = list(group_ids)
        self.account["revision"] = revision + 1
        return dict(self.account)

    async def restart_account(self, account_id: str) -> dict[str, object]:
        self.calls.append(("restart", account_id))
        return dict(self.account)

    async def test_model(self, account_id: str) -> dict[str, object]:
        self.calls.append(("model_test", account_id))
        return {"ok": True, "model": self.account["ai_model"]}

    async def clear_memory(self, account_id: str) -> int:
        self.calls.append(("clear_memory", account_id))
        return 7


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = FakeAccountManager()
        self.server = DashboardServer(
            username="admin",
            password="a-strong-dashboard-password",
            port=8000,
            manager=self.manager,  # type: ignore[arg-type]
        )

    def login(self, client: TestClient) -> str:
        response = client.post(
            "/api/login",
            json={
                "username": "admin",
                "password": "a-strong-dashboard-password",
            },
        )
        self.assertEqual(response.status_code, 200)
        return str(response.json()["csrf_token"])

    def test_auth_csrf_and_multi_account_controls(self) -> None:
        session_secret = "1AA-secret-telegram-session"
        api_key = "sk-secret-provider-key"
        with TestClient(self.server.app, base_url="https://testserver") as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/api/status").status_code, 401)

            csrf = self.login(client)
            status = client.get("/api/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.headers["x-csrf-token"], csrf)
            self.assertEqual(status.json()["memory_ttl_hours"], 24)

            self.assertEqual(
                client.post(
                    "/api/accounts/acct_one/control",
                    json={"enabled": False, "revision": 1},
                ).status_code,
                403,
            )
            headers = {"X-CSRF-Token": csrf}

            created = client.post(
                "/api/accounts",
                headers=headers,
                json={
                    "label": "第二個帳號",
                    "session_string": session_secret,
                    "gender": "male",
                    "stage": "old_member",
                    "task_name": "帶動自然話題",
                    "task_info": "關心群友近況",
                    "ai_base_url": "https://api.openai.com/v1",
                    "ai_api_key": api_key,
                    "ai_model": "gpt-5-mini",
                },
            )
            self.assertEqual(created.status_code, 201)
            self.assertNotIn(session_secret, created.text)
            self.assertNotIn(api_key, created.text)

            updated = client.put(
                "/api/accounts/acct_one",
                headers=headers,
                json={
                    "revision": 1,
                    "label": "主要帳號",
                    "task_name": "晚間互動",
                    "task_info": "依照目前群聊內容自然接話",
                    "ai_base_url": "https://openrouter.ai/api/v1",
                    "ai_model": "openai/gpt-5-mini",
                    "ai_api_key": "",
                    "clear_ai_api_key": False,
                },
            )
            self.assertEqual(updated.status_code, 200)
            self.assertNotIn("session_string", updated.text)
            self.assertNotIn("ai_api_key", updated.text)

            controlled = client.post(
                "/api/accounts/acct_one/control",
                headers=headers,
                json={"enabled": False, "revision": 2},
            )
            self.assertEqual(controlled.status_code, 200)
            self.assertFalse(controlled.json()["enabled"])

            groups = client.post(
                "/api/accounts/acct_one/groups",
                headers=headers,
                json={
                    "all_groups": False,
                    "group_ids": [-1001, -1002],
                    "revision": 3,
                },
            )
            self.assertEqual(groups.status_code, 200)
            self.assertEqual(groups.json()["group_ids"], [-1001, -1002])

            self.assertEqual(
                client.post(
                    "/api/accounts/acct_one/restart",
                    headers=headers,
                    json={},
                ).status_code,
                200,
            )
            model_test = client.post(
                "/api/accounts/acct_one/model/test",
                headers=headers,
                json={},
            )
            self.assertTrue(model_test.json()["ok"])
            cleared = client.post(
                "/api/accounts/acct_one/memory/clear",
                headers=headers,
                json={},
            )
            self.assertEqual(cleared.json()["removed"], 7)

    def test_login_rate_limit(self) -> None:
        with TestClient(self.server.app, base_url="https://testserver") as client:
            for attempt in range(5):
                response = client.post(
                    "/api/login",
                    headers={"X-Forwarded-For": "198.51.100.21"},
                    json={"username": "admin", "password": f"wrong-{attempt}"},
                )
                self.assertEqual(response.status_code, 401)
            blocked = client.post(
                "/api/login",
                headers={"X-Forwarded-For": "198.51.100.21"},
                json={
                    "username": "admin",
                    "password": "a-strong-dashboard-password",
                },
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertIn("Retry-After", blocked.headers)


if __name__ == "__main__":
    unittest.main()
