from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.dashboard import DashboardServer


class DashboardTests(unittest.TestCase):
    def test_login_status_and_controls(self) -> None:
        state: dict[str, object] = {
            "connected": True,
            "enabled": True,
            "joined_groups": [],
        }
        selected: dict[str, object] = {}

        async def status_provider() -> dict[str, object]:
            return state

        async def enabled_setter(enabled: bool) -> None:
            state["enabled"] = enabled

        async def group_filter_setter(
            all_groups: bool,
            group_ids: frozenset[int],
        ) -> None:
            selected["all_groups"] = all_groups
            selected["group_ids"] = group_ids

        async def memory_clearer() -> int:
            return 7

        server = DashboardServer(
            username="admin",
            password="a-strong-dashboard-password",
            port=8000,
            status_provider=status_provider,
            enabled_setter=enabled_setter,
            group_filter_setter=group_filter_setter,
            memory_clearer=memory_clearer,
        )

        with TestClient(server.app, base_url="https://testserver") as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/api/status").status_code, 401)
            self.assertEqual(
                client.post(
                    "/api/login",
                    json={"username": "admin", "password": "wrong-password"},
                ).status_code,
                401,
            )
            self.assertEqual(
                client.post(
                    "/api/login",
                    json={
                        "username": "admin",
                        "password": "a-strong-dashboard-password",
                    },
                ).status_code,
                200,
            )
            self.assertTrue(client.get("/api/status").json()["connected"])

            action_headers = {"X-Dashboard-Action": "1"}
            self.assertEqual(
                client.post(
                    "/api/control",
                    headers=action_headers,
                    json={"enabled": False},
                ).status_code,
                200,
            )
            self.assertFalse(state["enabled"])

            self.assertEqual(
                client.post(
                    "/api/groups",
                    headers=action_headers,
                    json={"all_groups": False, "group_ids": [-1001, -1002]},
                ).status_code,
                200,
            )
            self.assertEqual(selected["group_ids"], frozenset({-1001, -1002}))

            cleared = client.post(
                "/api/memory/clear",
                headers=action_headers,
                json={},
            )
            self.assertEqual(cleared.json()["removed"], 7)


if __name__ == "__main__":
    unittest.main()
