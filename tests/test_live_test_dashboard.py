from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.dashboard import Dashboard
from app.live_test import LiveTestError


class FakeLoginService:
    async def prune_expired(self):
        return None


class FakeManager:
    def __init__(self):
        self.workers = {}
        self.starts = []
        self.stop_calls = 0
        self.reject_start = False

    async def start_live_test(self, request):
        self.starts.append(request)
        if self.reject_start:
            raise LiveTestError("live test feature is disabled")
        return {"id": "run-1", "status": "running", "running": 4}

    async def live_test_status(self):
        return {"id": "run-1", "status": "running", "running": 4}

    async def stop_live_test(self):
        self.stop_calls += 1
        return {"id": "run-1", "status": "stopped", "running": 0}


def _dashboard():
    config = SimpleNamespace(dashboard_user="admin", dashboard_pass="secret")
    manager = FakeManager()
    dashboard = Dashboard(config, manager, FakeLoginService())
    return TestClient(dashboard.app), manager


def _request():
    return {
        "account_ids": ["a", "b", "c", "d"],
        "group_id": -1001,
        "duration_seconds": 3600,
        "event_cap": 40,
        "schedule": [{"event_id": "placeholder"}],
    }


def test_live_test_endpoints_require_dashboard_authentication():
    client, manager = _dashboard()
    with client:
        assert client.post("/api/live-test/start", json=_request()).status_code == 401
        assert client.get("/api/live-test/status").status_code == 401
        assert client.post("/api/live-test/stop").status_code == 401
        assert manager.starts == []
        assert manager.stop_calls == 0


def test_live_test_start_status_stop_endpoints_delegate_to_manager():
    client, manager = _dashboard()
    with client:
        login = client.post(
            "/api/login", json={"username": "admin", "password": "secret"}
        )
        assert login.status_code == 200

        request = _request()
        started = client.post("/api/live-test/start", json=request)
        assert started.status_code == 200
        assert started.json() == {
            "ok": True,
            "live_test": {"id": "run-1", "status": "running", "running": 4},
        }
        assert manager.starts == [request]

        status = client.get("/api/live-test/status")
        assert status.status_code == 200
        assert status.json()["live_test"]["running"] == 4

        stopped = client.post("/api/live-test/stop")
        assert stopped.status_code == 200
        assert stopped.json()["live_test"]["status"] == "stopped"
        assert stopped.json()["live_test"]["running"] == 0
        assert manager.stop_calls == 1


def test_live_test_start_endpoint_reports_invalid_and_fail_closed_requests():
    client, manager = _dashboard()
    with client:
        client.post(
            "/api/login", json={"username": "admin", "password": "secret"}
        )
        invalid = client.post("/api/live-test/start", json=[])
        assert invalid.status_code == 400
        assert manager.starts == []

        manager.reject_start = True
        rejected = client.post("/api/live-test/start", json=_request())
        assert rejected.status_code == 409
        assert "disabled" in rejected.json()["error"]
