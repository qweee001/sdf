from __future__ import annotations

import unittest
from typing import Any

from fastapi.testclient import TestClient

from app.dashboard import DASHBOARD_JS, DashboardServer


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
            "blocked_terms": ["測試屏蔽詞"],
            "blocked_topics": ["測試屏蔽主題"],
            "ai_base_url": "https://api.openai.com/v1",
            "ai_model": "gpt-5-mini",
            "has_custom_api_key": True,
            "session_configured": True,
            "all_groups": True,
            "group_ids": [],
            "joined_groups": [
                {"id": -1001, "title": "測試群組", "enabled": True},
            ],
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

    async def create_account(
        self,
        payload: dict[str, Any],
        owner_id: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(("create", (dict(payload), owner_id)))
        self.account.update(
            {
                "label": payload["label"],
                "task_name": payload["task_name"],
                "ai_model": payload["ai_model"],
                "blocked_terms": list(payload.get("blocked_terms", [])),
                "blocked_topics": list(payload.get("blocked_topics", [])),
                "revision": 1,
            }
        )
        return dict(self.account)

    async def start_phone_login(
        self,
        owner_id: str,
        phone: object,
    ) -> dict[str, object]:
        self.calls.append(("phone_start", (owner_id, phone)))
        return {
            "auth_id": "opaque-auth-id",
            "status": "code_required",
            "phone_hint": "+886•••••678",
            "expires_in": 600,
            "resend_after": 60,
        }

    async def submit_phone_code(
        self,
        owner_id: str,
        auth_id: object,
        code: object,
    ) -> dict[str, object]:
        self.calls.append(("phone_code", (owner_id, auth_id, code)))
        return {
            "auth_id": str(auth_id),
            "status": "password_required",
            "phone_hint": "+886•••••678",
            "expires_in": 500,
            "resend_after": 60,
        }

    async def submit_phone_password(
        self,
        owner_id: str,
        auth_id: object,
        password: object,
    ) -> dict[str, object]:
        self.calls.append(("phone_password", (owner_id, auth_id, password)))
        return {
            "auth_id": str(auth_id),
            "status": "authorized",
            "phone_hint": "+886•••••678",
            "expires_in": 300,
            "resend_after": 60,
        }

    async def cancel_phone_login(self, owner_id: str, auth_id: object) -> None:
        self.calls.append(("phone_cancel", (owner_id, auth_id)))

    async def cancel_phone_logins_for_owner(self, owner_id: str) -> int:
        self.calls.append(("phone_cancel_owner", owner_id))
        return 1

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
                "blocked_terms": list(payload.get("blocked_terms", [])),
                "blocked_topics": list(payload.get("blocked_topics", [])),
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

    async def conversation_log(
        self,
        account_id: str,
        group_id: int,
        limit: int,
    ) -> dict[str, object]:
        self.calls.append(("conversation_log", (account_id, group_id, limit)))
        return {
            "messages": [
                {
                    "sender_id": 987654321,
                    "sender_name": "<img src=x onerror=alert(1)>",
                    "role": "user",
                    "content": "<script>alert('xss')</script>",
                    "created_at": 1_700_000_000,
                },
                {
                    "sender_id": 123456,
                    "sender_name": "測試帳號",
                    "role": "assistant",
                    "content": "安全回覆",
                    "created_at": 1_700_000_001,
                },
            ][-limit:]
        }


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

            self.assertEqual(
                client.post(
                    "/api/telegram-auth/start",
                    json={"phone": "+886912345678"},
                ).status_code,
                403,
            )
            phone_started = client.post(
                "/api/telegram-auth/start",
                headers=headers,
                json={"phone": "+886912345678"},
            )
            self.assertEqual(phone_started.status_code, 201)
            self.assertEqual(phone_started.json()["status"], "code_required")
            self.assertNotIn("+886912345678", phone_started.text)

            code_result = client.post(
                "/api/telegram-auth/code",
                headers=headers,
                json={"auth_id": "opaque-auth-id", "code": "12345"},
            )
            self.assertEqual(code_result.status_code, 200)
            self.assertEqual(code_result.json()["status"], "password_required")
            self.assertNotIn("12345", code_result.text)

            password_result = client.post(
                "/api/telegram-auth/password",
                headers=headers,
                json={
                    "auth_id": "opaque-auth-id",
                    "password": "telegram-2fa-secret",
                },
            )
            self.assertEqual(password_result.status_code, 200)
            self.assertEqual(password_result.json()["status"], "authorized")
            self.assertNotIn("telegram-2fa-secret", password_result.text)

            cancelled = client.post(
                "/api/telegram-auth/cancel",
                headers=headers,
                json={"auth_id": "opaque-auth-id"},
            )
            self.assertEqual(cancelled.status_code, 200)

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
                    "blocked_terms": ["測試屏蔽詞"],
                    "blocked_topics": ["測試屏蔽主題"],
                },
            )
            self.assertEqual(created.status_code, 201)
            self.assertNotIn(session_secret, created.text)
            self.assertNotIn(api_key, created.text)
            self.assertEqual(created.json()["blocked_terms"], ["測試屏蔽詞"])
            self.assertEqual(created.json()["blocked_topics"], ["測試屏蔽主題"])

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
                    "blocked_terms": ["更新後屏蔽詞"],
                    "blocked_topics": ["更新後屏蔽主題"],
                },
            )
            self.assertEqual(updated.status_code, 200)
            self.assertNotIn("session_string", updated.text)
            self.assertNotIn("ai_api_key", updated.text)
            self.assertEqual(updated.json()["blocked_terms"], ["更新後屏蔽詞"])
            self.assertEqual(updated.json()["blocked_topics"], ["更新後屏蔽主題"])

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

    def test_conversation_log_auth_validation_and_public_shape(self) -> None:
        path = (
            "/api/accounts/acct_one/conversation-log"
            "?group_id=-1001&limit=100"
        )
        with TestClient(self.server.app, base_url="https://testserver") as client:
            self.assertEqual(client.get(path).status_code, 401)
            self.login(client)

            invalid_paths = [
                "/api/accounts/acct_one/conversation-log",
                "/api/accounts/acct_one/conversation-log?group_id=1001",
                "/api/accounts/acct_one/conversation-log?group_id=-010",
                "/api/accounts/acct_one/conversation-log?group_id=-1001&limit=0",
                "/api/accounts/acct_one/conversation-log?group_id=-1001&limit=101",
                "/api/accounts/acct_one/conversation-log?group_id=-1001&limit=1.5",
                "/api/accounts/acct_one/conversation-log?group_id=-1001&limit=01",
                (
                    "/api/accounts/acct_one/conversation-log"
                    "?group_id=-1001&group_id=-1002&limit=20"
                ),
            ]
            for invalid_path in invalid_paths:
                with self.subTest(path=invalid_path):
                    self.assertEqual(client.get(invalid_path).status_code, 400)

            response = client.get(path)
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result["account_id"], "acct_one")
            self.assertEqual(result["group_id"], -1001)
            self.assertEqual(result["count"], 2)
            self.assertEqual(len(result["messages"]), 2)
            self.assertNotIn("sender_id", response.text)
            self.assertEqual(
                result["messages"][0]["content"],
                "<script>alert('xss')</script>",
            )
            self.assertIn(
                ("conversation_log", ("acct_one", -1001, 100)),
                self.manager.calls,
            )

        self.assertNotIn("innerHTML", DASHBOARD_JS)
        self.assertIn("body.textContent", DASHBOARD_JS)
        self.assertIn("sender.textContent", DASHBOARD_JS)

    def test_conversation_log_discards_stale_responses(self) -> None:
        self.assertIn(
            "const requestSequence = ++conversationRequestSequence;",
            DASHBOARD_JS,
        )
        self.assertIn(
            "requestSequence === conversationRequestSequence",
            DASHBOARD_JS,
        )
        self.assertIn(
            "requestKey === conversationSelectionKey",
            DASHBOARD_JS,
        )
        self.assertIn(
            "if (!conversationRequestIsCurrent(requestSequence, requestKey)) return;\n"
            "    conversationLoadedKey = requestKey;\n"
            "    renderConversationLog(result);",
            DASHBOARD_JS,
        )
        self.assertIn(
            "} catch (error) {\n"
            "    if (!conversationRequestIsCurrent(requestSequence, requestKey)) return;\n"
            '    setNotice("conversationNotice", error.message, "error");',
            DASHBOARD_JS,
        )

        handler_start = DASHBOARD_JS.index(
            '$("refreshConversationButton").addEventListener'
        )
        handler_end = DASHBOARD_JS.index(
            '$("clearMemoryButton").addEventListener',
            handler_start,
        )
        handler = DASHBOARD_JS[handler_start:handler_end]
        success_guard = handler.index(
            "if (!conversationRequestIsCurrent(requestSequence, requestKey)) return;"
        )
        loaded_key_write = handler.index("conversationLoadedKey = requestKey;")
        render = handler.index("renderConversationLog(result);")
        self.assertLess(success_guard, loaded_key_write)
        self.assertLess(success_guard, render)


if __name__ == "__main__":
    unittest.main()
