from __future__ import annotations

import unittest
from typing import Any

from fastapi.testclient import TestClient

from app.dashboard import DASHBOARD_HTML, DASHBOARD_JS, DashboardServer


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
            "adult_text_enabled": False,
            "has_custom_api_key": True,
            "image_api_key": "must-never-leave-the-server",
            "media_providers": {
                "openrouter_media": True,
                "azure_speech": True,
            },
            "media": {
                "image": {
                    "enabled": True,
                    "model": "image-model-v1",
                    "voice": "",
                    "daily_limit": 12,
                    "cooldown_seconds": 90,
                    "allowed_group_ids": [-1001],
                },
                "voice": {
                    "enabled": True,
                    "model": "speech-model-v1",
                    "voice": "zh-TW-HsiaoChenNeural",
                    "daily_limit": 20,
                    "cooldown_seconds": 30,
                    "allowed_group_ids": [-1001],
                },
                "video": {
                    "enabled": False,
                    "model": "video-model-v1",
                    "voice": "",
                    "daily_limit": 3,
                    "cooldown_seconds": 600,
                    "allowed_group_ids": [],
                },
            },
            "session_configured": True,
            "all_groups": True,
            "group_ids": [],
            "joined_groups": [
                {"id": -1001, "title": "測試群組", "enabled": True},
            ],
            "revision": 1,
            "state": "connected",
            "private_unread_count": 2,
            "private_alert_latest_at": 1_700_000_001,
        }

    async def status(self) -> dict[str, object]:
        return {
            "ok": True,
            "account_count": 1,
            "memory_ttl_hours": 24,
            "summary": {
                "total": 1,
                "enabled": 1,
                "connected": 1,
                "memory_ttl_hours": 24,
                "private_unread_count": 2,
                "private_alert_latest_at": 1_700_000_001,
            },
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
                "adult_text_enabled": bool(
                    payload.get("adult_text_enabled", False)
                ),
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
                "adult_text_enabled": bool(
                    payload.get(
                        "adult_text_enabled",
                        self.account["adult_text_enabled"],
                    )
                ),
                "blocked_terms": list(payload.get("blocked_terms", [])),
                "blocked_topics": list(payload.get("blocked_topics", [])),
                "revision": int(payload["revision"]) + 1,
            }
        )
        if "media" in payload:
            self.account["media"] = payload["media"]
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

    async def manual_send_text(
        self,
        account_id: str,
        group_id: int,
        text: str,
    ) -> dict[str, object]:
        self.calls.append(("manual_send_text", (account_id, group_id, text)))
        return {
            "ok": True,
            "partial": False,
            "message_ids": [901],
            "message_count": 1,
            "sent_utf16_units": len(text.encode("utf-16-le")) // 2,
            "image_api_key": "must-never-leave-the-server",
        }

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

    async def media_jobs(
        self,
        account_id: str,
        limit: int,
    ) -> dict[str, object]:
        self.calls.append(("media_jobs", (account_id, limit)))
        return {
            "jobs": [
                {
                    "job_id": "job-001",
                    "media_type": "image",
                    "status": "completed",
                    "group_id": -1001,
                    "created_at": 1_700_000_000,
                    "updated_at": 1_700_000_010,
                    "prompt": "<script>alert('job')</script>",
                    "image_api_key": "job-provider-secret",
                    "result_url": "https://private.example/result.png",
                },
                {
                    "id": "job-002",
                    "media_type": "voice",
                    "status": "queued",
                    "group_id": -1001,
                    "created_at": 1_700_000_020,
                    "updated_at": 0,
                },
            ][-limit:]
        }

    async def private_alerts(
        self,
        account_id: str,
        *,
        limit: int = 50,
        unread_only: bool = False,
    ) -> dict[str, object]:
        self.calls.append(
            ("private_alerts", (account_id, limit, unread_only))
        )
        alerts = [
            {
                "alert_id": "11111111-1111-4111-8111-111111111111",
                "sender_id": 987654321,
                "sender_name": "<img src=x onerror=alert(1)>",
                "preview": "<script>alert('private')</script>",
                "created_at": 1_700_000_000,
                "acknowledged": False,
                "raw_text": "must-never-leave-the-server",
            },
            {
                "alert_id": "22222222-2222-4222-8222-222222222222",
                "sender_id": 123456789,
                "sender_name": "測試私聊",
                "preview": "已讀訊息",
                "created_at": 1_700_000_001,
                "acknowledged": True,
            },
        ]
        if unread_only:
            alerts = [alert for alert in alerts if not alert["acknowledged"]]
        return {
            "account_id": account_id,
            "unread_count": 1,
            "latest_at": 1_700_000_001,
            "alerts": alerts[:limit],
        }

    async def acknowledge_private_alerts(
        self,
        account_id: str,
        *,
        alert_ids: list[str] | None = None,
        acknowledge_all: bool = False,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "acknowledge_private_alerts",
                (account_id, alert_ids, acknowledge_all),
            )
        )
        return {
            "account_id": account_id,
            "acknowledged": 2 if acknowledge_all else len(alert_ids or []),
            "unread_count": 0,
            "latest_at": 1_700_000_001,
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
            self.assertNotIn("has_custom_api_key", status.text)
            self.assertNotIn("image_api_key", status.text)
            self.assertTrue(
                status.json()["accounts"][0]["media_providers"]["openrouter_media"]
            )

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
                    "ai_model": "gpt-5-mini",
                    "adult_text_enabled": True,
                    "blocked_terms": ["測試屏蔽詞"],
                    "blocked_topics": ["測試屏蔽主題"],
                },
            )
            self.assertEqual(created.status_code, 201)
            self.assertNotIn(session_secret, created.text)
            self.assertNotIn(api_key, created.text)
            self.assertNotIn("has_custom_api_key", created.text)
            self.assertNotIn("image_api_key", created.text)
            self.assertEqual(created.json()["blocked_terms"], ["測試屏蔽詞"])
            self.assertEqual(created.json()["blocked_topics"], ["測試屏蔽主題"])
            self.assertTrue(created.json()["adult_text_enabled"])

            rejected_key = client.put(
                "/api/accounts/acct_one",
                headers=headers,
                json={"revision": 1, "image_api_key": api_key},
            )
            self.assertEqual(rejected_key.status_code, 400)
            self.assertNotIn(api_key, rejected_key.text)

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
                    "adult_text_enabled": False,
                    "blocked_terms": ["更新後屏蔽詞"],
                    "blocked_topics": ["更新後屏蔽主題"],
                    "media": {
                        "image": {
                            "enabled": True,
                            "model": "image-v2",
                            "voice": "",
                            "daily_limit": 10,
                            "cooldown_seconds": 120,
                            "allowed_group_ids": [-1001, -1002],
                        },
                        "voice": {
                            "enabled": True,
                            "model": "speech-v2",
                            "voice": "zh-TW-HsiaoChenNeural",
                            "daily_limit": 25,
                            "cooldown_seconds": 45,
                            "allowed_group_ids": [-1001],
                        },
                        "video": {
                            "enabled": False,
                            "model": "video-v2",
                            "voice": "",
                            "daily_limit": 2,
                            "cooldown_seconds": 900,
                            "allowed_group_ids": [-1002],
                        },
                    },
                },
            )
            self.assertEqual(updated.status_code, 200)
            self.assertNotIn("session_string", updated.text)
            self.assertNotIn("ai_api_key", updated.text)
            self.assertNotIn("image_api_key", updated.text)
            self.assertEqual(updated.json()["blocked_terms"], ["更新後屏蔽詞"])
            self.assertEqual(updated.json()["blocked_topics"], ["更新後屏蔽主題"])
            self.assertFalse(updated.json()["adult_text_enabled"])
            self.assertEqual(
                updated.json()["media"]["image"]["allowed_group_ids"],
                [-1001, -1002],
            )
            self.assertEqual(
                updated.json()["media"]["voice"]["voice"],
                "zh-TW-HsiaoChenNeural",
            )

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

    def test_private_alerts_auth_validation_csrf_and_public_shape(self) -> None:
        get_path = (
            "/api/accounts/acct_one/private-alerts"
            "?limit=20&unread_only=false"
        )
        ack_path = "/api/accounts/acct_one/private-alerts/ack"
        with TestClient(self.server.app, base_url="https://testserver") as client:
            self.assertEqual(client.get(get_path).status_code, 401)
            self.assertEqual(
                client.post(ack_path, json={"all": True}).status_code,
                401,
            )
            csrf = self.login(client)
            self.assertEqual(
                client.post(ack_path, json={"all": True}).status_code,
                403,
            )

            invalid_get_paths = [
                "/api/accounts/acct_one/private-alerts?limit=0",
                "/api/accounts/acct_one/private-alerts?limit=101",
                "/api/accounts/acct_one/private-alerts?limit=01",
                "/api/accounts/acct_one/private-alerts?unread_only=1",
                "/api/accounts/acct_one/private-alerts?unread_only=True",
                (
                    "/api/accounts/acct_one/private-alerts"
                    "?limit=20&limit=50"
                ),
                (
                    "/api/accounts/acct_one/private-alerts"
                    "?unread_only=true&unread_only=false"
                ),
                "/api/accounts/acct_one/private-alerts?extra=true",
            ]
            for path in invalid_get_paths:
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 400)

            response = client.get(get_path)
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result["account_id"], "acct_one")
            self.assertEqual(result["unread_count"], 1)
            self.assertEqual(result["count"], 2)
            self.assertEqual(
                result["alerts"][0]["alert_id"],
                "11111111-1111-4111-8111-111111111111",
            )
            self.assertNotIn("sender_id", response.text)
            self.assertNotIn("raw_text", response.text)
            self.assertNotIn("must-never-leave-the-server", response.text)
            self.assertIn(
                ("private_alerts", ("acct_one", 20, False)),
                self.manager.calls,
            )

            headers = {"X-CSRF-Token": csrf}
            invalid_payloads = [
                {},
                {"all": False},
                {
                    "all": True,
                    "alert_ids": ["11111111-1111-4111-8111-111111111111"],
                },
                {"alert_ids": []},
                {"alert_ids": "11111111-1111-4111-8111-111111111111"},
                {"alert_ids": [1]},
                {"alert_ids": [""]},
                {
                    "alert_ids": [
                        "11111111-1111-4111-8111-111111111111",
                        "11111111-1111-4111-8111-111111111111",
                    ]
                },
                {"alert_ids": ["x" * 161]},
            ]
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    self.assertEqual(
                        client.post(
                            ack_path,
                            headers=headers,
                            json=payload,
                        ).status_code,
                        400,
                    )

            one = client.post(
                ack_path,
                headers=headers,
                json={
                    "alert_ids": [
                        "11111111-1111-4111-8111-111111111111"
                    ]
                },
            )
            self.assertEqual(one.status_code, 200)
            self.assertEqual(one.json()["acknowledged"], 1)
            self.assertIn(
                (
                    "acknowledge_private_alerts",
                    (
                        "acct_one",
                        ["11111111-1111-4111-8111-111111111111"],
                        False,
                    ),
                ),
                self.manager.calls,
            )

            all_alerts = client.post(
                ack_path,
                headers=headers,
                json={"all": True},
            )
            self.assertEqual(all_alerts.status_code, 200)
            self.assertEqual(all_alerts.json()["acknowledged"], 2)
            self.assertIn(
                (
                    "acknowledge_private_alerts",
                    ("acct_one", None, True),
                ),
                self.manager.calls,
            )

    def test_private_alert_ui_is_compact_safe_and_stale_guarded(self) -> None:
        for field_id in (
            "summaryPrivateUnread",
            "metricPrivateUnread",
            "refreshPrivateAlertsButton",
            "ackAllPrivateAlertsButton",
            "privateAlertsNotice",
            "privateAlertList",
        ):
            with self.subTest(field_id=field_id):
                self.assertIn(f'id="{field_id}"', DASHBOARD_HTML)
        self.assertIn("最近 24 小時收到的私聊提示", DASHBOARD_HTML)
        self.assertIn("AI 不會在私聊自動回覆", DASHBOARD_HTML)
        self.assertIn("不會向 Telegram 傳送已讀回條", DASHBOARD_HTML)
        self.assertIn("全部標記已處理", DASHBOARD_HTML)
        self.assertIn("private-alert-badge", DASHBOARD_HTML)
        self.assertIn("/private-alerts?", DASHBOARD_JS)
        self.assertIn("/private-alerts/ack", DASHBOARD_JS)
        self.assertIn(
            "const requestSequence = ++privateAlertsRequestSequence;",
            DASHBOARD_JS,
        )
        self.assertIn(
            "requestSequence !== privateAlertsRequestSequence",
            DASHBOARD_JS,
        )
        self.assertIn("selectedAccountId !== accountId", DASHBOARD_JS)
        self.assertIn("sender.textContent", DASHBOARD_JS)
        self.assertIn("preview.textContent", DASHBOARD_JS)
        self.assertIn("Telegram AI 多帳號控制台", DASHBOARD_JS)
        self.assertIn('acknowledge.textContent = "標記已處理"', DASHBOARD_JS)
        self.assertIn("async function refreshPrivateIndicators()", DASHBOARD_JS)
        self.assertIn("privateIndicatorRefreshPending", DASHBOARD_JS)
        self.assertIn("refreshPrivateIndicators().catch", DASHBOARD_JS)
        self.assertIn("function resetDashboardClientState()", DASHBOARD_JS)
        self.assertIn("privateAlertsRequestSequence += 1", DASHBOARD_JS)
        self.assertIn("clearPrivateAlerts();", DASHBOARD_JS)
        self.assertNotIn("innerHTML", DASHBOARD_JS)

    def test_media_jobs_auth_validation_and_public_shape(self) -> None:
        path = "/api/accounts/acct_one/media-jobs?limit=20"
        with TestClient(self.server.app, base_url="https://testserver") as client:
            self.assertEqual(client.get(path).status_code, 401)
            self.login(client)

            invalid_paths = [
                "/api/accounts/acct_one/media-jobs?limit=0",
                "/api/accounts/acct_one/media-jobs?limit=101",
                "/api/accounts/acct_one/media-jobs?limit=01",
                "/api/accounts/acct_one/media-jobs?limit=20&limit=50",
            ]
            for invalid_path in invalid_paths:
                with self.subTest(path=invalid_path):
                    self.assertEqual(client.get(invalid_path).status_code, 400)

            response = client.get(path)
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result["account_id"], "acct_one")
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["jobs"][0]["job_id"], "job-001")
            self.assertEqual(result["jobs"][0]["kind"], "image")
            self.assertEqual(result["jobs"][1]["job_id"], "job-002")
            self.assertNotIn("prompt", response.text)
            self.assertNotIn("result_url", response.text)
            self.assertNotIn("api_key", response.text)
            self.assertNotIn("job-provider-secret", response.text)
            self.assertIn(
                ("media_jobs", ("acct_one", 20)),
                self.manager.calls,
            )

    def test_media_ui_contract_uses_railway_keys_and_safe_dom(self) -> None:
        for field_id in (
            "editImageEnabled",
            "editImageModel",
            "editImageDailyLimit",
            "editImageCooldown",
            "editImageGroupIds",
            "editImageKnownGroups",
            "editVoiceEnabled",
            "editVoiceModel",
            "editVoiceName",
            "editVoiceDailyLimit",
            "editVoiceCooldown",
            "editVoiceGroupIds",
            "editVoiceKnownGroups",
            "editVideoEnabled",
            "editVideoModel",
            "editVideoDailyLimit",
            "editVideoCooldown",
            "editVideoGroupIds",
            "editVideoKnownGroups",
            "imageProviderState",
            "videoProviderState",
            "voiceProviderState",
            "refreshMediaJobsButton",
            "mediaJobList",
            "addAdultTextEnabled",
            "editAdultTextEnabled",
        ):
            with self.subTest(field_id=field_id):
                self.assertIn(f'id="{field_id}"', DASHBOARD_HTML)

        self.assertIn("Railway Variables", DASHBOARD_HTML)
        self.assertIn("成人純文字模式", DASHBOARD_HTML)
        self.assertNotIn('id="addApiKey"', DASHBOARD_HTML)
        self.assertNotIn('id="editApiKey"', DASHBOARD_HTML)
        self.assertNotIn('id="clearApiKey"', DASHBOARD_HTML)
        self.assertNotIn("ai_api_key", DASHBOARD_JS)
        self.assertNotIn("clear_ai_api_key", DASHBOARD_JS)
        self.assertIn("media: {\n          image: {", DASHBOARD_JS)
        self.assertIn(
            'adult_text_enabled: $("addAdultTextEnabled").checked',
            DASHBOARD_JS,
        )
        self.assertIn(
            'adult_text_enabled: $("editAdultTextEnabled").checked',
            DASHBOARD_JS,
        )
        self.assertIn(
            'allowed_group_ids: mediaGroupIds("Image"',
            DASHBOARD_JS,
        )
        self.assertIn(
            'allowed_group_ids: mediaGroupIds("Voice"',
            DASHBOARD_JS,
        )
        self.assertIn(
            'allowed_group_ids: mediaGroupIds("Video"',
            DASHBOARD_JS,
        )
        self.assertEqual(DASHBOARD_JS.count("providers.openrouter_media"), 3)
        self.assertIn("/media-jobs?", DASHBOARD_JS)
        self.assertIn("const requestSequence = ++mediaJobsRequestSequence;", DASHBOARD_JS)
        self.assertIn("selectedAccountId !== accountId", DASHBOARD_JS)
        self.assertIn("title.textContent", DASHBOARD_JS)
        self.assertIn("meta.textContent", DASHBOARD_JS)
        self.assertNotIn("innerHTML", DASHBOARD_JS)

    def test_manual_message_auth_csrf_validation_and_send(self) -> None:
        path = "/api/accounts/acct_one/manual-message"
        valid_payload = {"group_id": -1001, "text": "  大家晚安  "}
        with TestClient(self.server.app, base_url="https://testserver") as client:
            self.assertEqual(client.post(path, json=valid_payload).status_code, 401)
            csrf = self.login(client)
            self.assertEqual(client.post(path, json=valid_payload).status_code, 403)
            headers = {"X-CSRF-Token": csrf}

            invalid_payloads = [
                {},
                {"group_id": -1001},
                {"text": "訊息"},
                {"group_id": True, "text": "訊息"},
                {"group_id": "-1001", "text": "訊息"},
                {"group_id": 1001, "text": "訊息"},
                {"group_id": -(2**63) - 1, "text": "訊息"},
                {"group_id": -1001, "text": None},
                {"group_id": -1001, "text": "   "},
                {"group_id": -1001, "text": "訊息", "extra": True},
                {"group_id": -1001, "text": "訊息", "image_api_key": "secret"},
            ]
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    response = client.post(path, headers=headers, json=payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertNotIn("secret", response.text)

            oversized = client.post(
                path,
                headers=headers,
                json={"group_id": -1001, "text": "x" * (128 * 1024)},
            )
            self.assertEqual(oversized.status_code, 400)

            sent = client.post(path, headers=headers, json=valid_payload)
            self.assertEqual(sent.status_code, 200)
            self.assertEqual(sent.json()["message_ids"], [901])
            self.assertEqual(sent.json()["message_count"], 1)
            self.assertNotIn("image_api_key", sent.text)
            self.assertIn(
                (
                    "manual_send_text",
                    ("acct_one", -1001, "  大家晚安  "),
                ),
                self.manager.calls,
            )

    def test_compact_manual_send_and_collapsible_ui_contract(self) -> None:
        self.assertIn("manual-send-row", DASHBOARD_HTML)
        self.assertIn("manual-send-button", DASHBOARD_HTML)
        self.assertIn('data-panel-title="帳號概覽"', DASHBOARD_HTML)
        self.assertEqual(DASHBOARD_HTML.count("data-collapsible"), 1)
        self.assertEqual(DASHBOARD_HTML.count('data-expanded="false"'), 1)
        self.assertNotIn('data-expanded="true"', DASHBOARD_HTML)
        self.assertGreaterEqual(DASHBOARD_HTML.count('class="account-section'), 6)
        for element_id in (
            "settingsForm",
            "mediaJobList",
            "groupList",
            "conversationList",
            "clearMemoryButton",
        ):
            self.assertIn(f'id="{element_id}"', DASHBOARD_HTML)
        self.assertIn('class="field full">任務名稱', DASHBOARD_HTML)
        self.assertIn('toggle.setAttribute("aria-expanded"', DASHBOARD_JS)
        self.assertIn('toggle.setAttribute("aria-controls"', DASHBOARD_JS)
        self.assertIn("content.hidden = !nextExpanded", DASHBOARD_JS)
        self.assertIn('summary.id = "selectedCompactSummary"', DASHBOARD_JS)
        self.assertIn('$("selectedCompactSummary").textContent', DASHBOARD_JS)
        self.assertIn('account.ai_model || "未設定模型"', DASHBOARD_JS)
        self.assertIn('account.adult_text_enabled ? "成人純文字開"', DASHBOARD_JS)
        self.assertIn("${groupCount} 個群組", DASHBOARD_JS)
        self.assertIn("function createManualSendRow(account)", DASHBOARD_JS)
        self.assertIn("manualGroupByAccount", DASHBOARD_JS)
        self.assertIn("manualMessageDraftByAccount", DASHBOARD_JS)
        self.assertIn("manualMessagePendingAccounts", DASHBOARD_JS)
        self.assertIn('message.className = "manual-message-input"', DASHBOARD_JS)
        self.assertIn("function activeManualMessageEditor()", DASHBOARD_JS)
        self.assertIn(
            "if (activeManualMessageEditor() || "
            "manualMessagePendingAccounts.size > 0) return;",
            DASHBOARD_JS,
        )
        self.assertIn("function dashboardEditorHasFocus()", DASHBOARD_JS)
        self.assertIn("!dashboardEditorHasFocus()", DASHBOARD_JS)
        self.assertIn("if (!formDirty) fillEditor(account);", DASHBOARD_JS)
        self.assertIn("if (!groupsDirty) renderGroups(account);", DASHBOARD_JS)
        self.assertNotIn("message.maxLength", DASHBOARD_JS)
        self.assertIn("group?.enabled !== false", DASHBOARD_JS)
        self.assertIn(
            "sendButton.disabled = !account.connected || !groups.length || pending",
            DASHBOARD_JS,
        )
        self.assertIn("manualMessagePendingAccounts.size === 0", DASHBOARD_JS)
        self.assertIn("/manual-message", DASHBOARD_JS)
        self.assertIn("body: JSON.stringify({group_id: groupId, text})", DASHBOARD_JS)
        self.assertIn("if (result.partial === true)", DASHBOARD_JS)
        self.assertIn("text.slice(safeUnits)", DASHBOARD_JS)
        self.assertIn("剩餘內容已保留", DASHBOARD_JS)
        self.assertIn("notice.textContent = error.message", DASHBOARD_JS)
        self.assertNotIn("innerHTML", DASHBOARD_JS)

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
