"""群組總管：群名解析、備註名、活動聚合、目錄 API、一鍵加入/移出。

對應控制台「找個群組都麻煩」的三個痛點：
1. 群名解析（Telethon get_display_name(Dialog) 一律回空字串 → 全部變成「群組 -100xxxx」）
2. 群清單沒有活動資訊、不能搜尋、要一個帳號一個帳號勾
3. 剛被拉進去的群要等帳號重啟才看得到
"""

import asyncio
import os
import tempfile
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import load_settings
from app.crypto import SecretBox
from app.dashboard import Dashboard
from app.database import Database
from app.manager import AccountManager
from app.telegram_login import TelegramLoginService
from app.worker import _dialog_member_count, _dialog_title

_DB_DIR = os.path.join(tempfile.gettempdir(), "sdf_test")

GROUP_A = -1002229799107
GROUP_B = -5565520321
ACC_1 = "acc1"
ACC_2 = "acc2"


# ---------- 群名解析 ----------


class _FakeChat:
    def __init__(self, title, members):
        self.title = title
        self.participants_count = members


class _FakeDialog:
    """模仿 Telethon Dialog：title 是預先算好的顯示名，entity 才是本體。"""

    def __init__(self, gid, title, members):
        self.id = gid
        self.title = title
        self.name = title
        self.entity = _FakeChat(title, members)
        self.is_group = True


def test_dialog_title_uses_dialog_title_when_present():
    dialog = _FakeDialog(GROUP_A, "桃花源・約會", 42)
    assert _dialog_title(dialog) == "桃花源・約會"
    assert _dialog_member_count(dialog) == 42


def test_dialog_title_falls_back_to_entity_when_dialog_title_empty():
    """Dialog.title 空的時候要往下找 entity，而不是直接退成「群組 <id>」。"""
    dialog = SimpleNamespace(id=GROUP_B, title="", name="", entity=_FakeChat("測試群", 7))
    assert _dialog_title(dialog) == "測試群"
    assert _dialog_member_count(dialog) == 7


def test_dialog_title_falls_back_to_id_when_nothing_known():
    dialog = SimpleNamespace(id=GROUP_B, title="", name="", entity=None)
    assert _dialog_title(dialog) == f"群組 {GROUP_B}"
    assert _dialog_member_count(dialog) == 0


# ---------- 控制台測試骨架 ----------


class _FakeWorker:
    """只提供目錄需要的介面。"""

    def __init__(self, groups, selected):
        self._groups = list(groups)
        self.selected_groups = set(selected)
        self.is_running = True
        self.stats = {"replies_sent": 0, "errors": 0, "proactive_sent": 0}
        self.refresh_calls = 0

    def group_list(self):
        return [
            {
                "id": gid,
                "title": title,
                "members": members,
                "selected": gid in self.selected_groups,
            }
            for gid, title, members in self._groups
        ]

    async def refresh_dialogs(self, *, max_age: float = 60.0) -> bool:
        self.refresh_calls += 1
        return True

    def update_selected_groups(self, groups):
        self.selected_groups = set(groups)

    async def stop(self):
        self.is_running = False


def _make_dashboard():
    """回傳 (client, db, manager, loop)：測試可直接操作 DB 與 worker。"""
    os.makedirs(_DB_DIR, exist_ok=True)
    db_path = os.path.join(_DB_DIR, f"hub_test_{uuid.uuid4().hex}.db")
    s = load_settings()
    box = SecretBox(s.account_encryption_key)
    db = Database(db_path)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(db.connect())

    manager = AccountManager(s, db, box)
    login = TelegramLoginService(s.tg_api_id, s.tg_api_hash)
    dash = Dashboard(s, manager, login)

    client = TestClient(dash.app)

    def _close():
        try:
            loop.run_until_complete(manager.aclose())
            loop.run_until_complete(db.close())
        finally:
            loop.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(db_path + suffix)
                except OSError:
                    pass

    def _exit(exc_type, exc, tb):
        try:
            return client.__exit__(exc_type, exc, tb)
        finally:
            _close()

    client.__exit__ = _exit
    return client, db, manager, loop


def _setup_account(db, loop, manager, account_id, *, groups, worker_groups, selected):
    """建帳號 + 掛假 worker + 記錄訊息，讓目錄有東西可看。"""
    loop.run_until_complete(
        db.create_account(account_id, f"水軍 {account_id}", "session", persona='{"name": "小小"}')
    )
    loop.run_until_complete(
        db.update_account(account_id, groups=_json_int_list(groups), setup_complete=1, enabled=1)
    )
    worker = _FakeWorker(worker_groups, selected)
    manager.workers[account_id] = worker
    return worker


def _json_int_list(values):
    import json

    return json.dumps([int(v) for v in values])


def _insert_message(db, loop, account_id, group_id, sender_id, name, role, content, ts):
    """直接寫入指定時間戳的訊息（add_message 的時間是內部 time.time()）。"""

    async def _run():
        await db._c.execute(
            "INSERT INTO messages (account_id, group_id, sender_id, sender_name, role, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, int(group_id), int(sender_id), name, role, content, float(ts)),
        )
        await db._c.commit()

    loop.run_until_complete(_run())


def _login(client):
    r = client.post("/api/login", json={"username": "admin", "password": "secret123"})
    assert r.status_code == 200


# ---------- 備註名 ----------


def test_group_label_roundtrip_and_clear():
    client, db, manager, loop = _make_dashboard()
    with client:
        _login(client)
        r = client.post("/api/groups/label", json={"group_id": GROUP_A, "label": "桃花源"})
        assert r.status_code == 200 and r.json()["label"] == "桃花源"
        assert loop.run_until_complete(db.get_group_labels()) == {GROUP_A: "桃花源"}

        r = client.post("/api/groups/label", json={"group_id": GROUP_A, "label": "桃花源 v2"})
        assert r.json()["label"] == "桃花源 v2"
        assert loop.run_until_complete(db.get_group_labels()) == {GROUP_A: "桃花源 v2"}

        # 空字串＝清除
        r = client.post("/api/groups/label", json={"group_id": GROUP_A, "label": "  "})
        assert r.status_code == 200
        assert loop.run_until_complete(db.get_group_labels()) == {}


def test_group_label_rejects_bad_payload_and_requires_login():
    client, db, manager, loop = _make_dashboard()
    with client:
        assert client.post(
            "/api/groups/label", json={"group_id": GROUP_A, "label": "x"}
        ).status_code == 401
        _login(client)
        assert client.post(
            "/api/groups/label", json={"group_id": "abc", "label": "x"}
        ).status_code == 400
        assert client.post(
            "/api/groups/label", json={"group_id": GROUP_A, "label": 123}
        ).status_code == 400


# ---------- 活動聚合 ----------


def test_group_overview_counts_humans_and_last_activity():
    import time

    client, db, manager, loop = _make_dashboard()
    with client:
        now = time.time()
        rows = [
            (ACC_1, GROUP_A, 1, "阿宏", "user", "剛下班", now - 900),
            (ACC_1, GROUP_A, 2, "美玲", "user", "要一起吃飯嗎", now - 600),
            (ACC_1, GROUP_A, 9, "小小", "assistant", "好呀", now - 120),
            (ACC_2, GROUP_B, 3, "阿明", "user", "有人在嗎", now - 300),
        ]
        for account_id, gid, sender_id, name, role, content, ts in rows:
            _insert_message(db, loop, account_id, gid, sender_id, name, role, content, ts)

        overview = {r["group_id"]: r for r in loop.run_until_complete(db.group_overview())}
        group_a = overview[GROUP_A]
        assert group_a["msg_count"] == 3
        assert group_a["human_senders"] == 2  # 水軍不計入真人數
        assert abs(group_a["last_ts"] - (now - 120)) < 0.01  # 最後一則（水軍）
        assert abs(group_a["last_human_ts"] - (now - 600)) < 0.01  # 最後一則真人
        assert overview[GROUP_B]["human_senders"] == 1


# ---------- 目錄 API ----------


def test_groups_directory_merges_accounts_titles_labels_and_activity():
    import time

    client, db, manager, loop = _make_dashboard()
    with client:
        _login(client)
        _setup_account(
            db, loop, manager, ACC_1,
            groups=[GROUP_A], worker_groups=[(GROUP_A, "桃花源・約會", 120), (GROUP_B, "測試群", 8)],
            selected=[GROUP_A],
        )
        _setup_account(
            db, loop, manager, ACC_2,
            groups=[GROUP_B], worker_groups=[(GROUP_B, "測試群", 8)], selected=[GROUP_B],
        )
        loop.run_until_complete(db.upsert_group_label(GROUP_B, "我的測試群"))
        now = time.time()
        _insert_message(db, loop, ACC_1, GROUP_A, 1, "阿宏", "user", "在嗎", now - 60)
        _insert_message(db, loop, ACC_2, GROUP_B, 2, "阿明", "user", "安安", now - 10)

        r = client.get("/api/groups/directory")
        assert r.status_code == 200
        data = r.json()
        groups = {g["id"]: g for g in data["groups"]}

        assert [a["id"] for a in data["accounts"]] == [ACC_1, ACC_2]
        assert data["accounts"][0]["persona_name"] == "小小"

        assert groups[GROUP_A]["title"] == "桃花源・約會"
        assert groups[GROUP_A]["members"] == 120
        assert groups[GROUP_A]["accounts"] == [ACC_1]
        assert groups[GROUP_A]["selected_count"] == 1
        assert groups[GROUP_A]["msg_count"] == 1
        assert groups[GROUP_A]["human_senders"] == 1

        # 有備註名的群用備註當顯示名，但 Telegram 原名仍保留
        assert groups[GROUP_B]["display"] == "我的測試群"
        assert groups[GROUP_B]["label"] == "我的測試群"
        assert groups[GROUP_B]["title"] == "測試群"
        assert groups[GROUP_B]["accounts"] == [ACC_2]

        # 預設排序：最近有訊息的排前面（GROUP_B 剛剛才有人講話）
        assert data["groups"][0]["id"] == GROUP_B


def test_groups_directory_includes_groups_without_live_worker():
    """只有歷史訊息、帳號沒掛 worker 的群也要看得到（不然找不到舊群）。"""
    import time

    client, db, manager, loop = _make_dashboard()
    with client:
        _login(client)
        _insert_message(
            db, loop, ACC_1, GROUP_A, 1, "阿宏", "user", "舊訊息", time.time() - 7200
        )
        r = client.get("/api/groups/directory")
        groups = {g["id"]: g for g in r.json()["groups"]}
        assert GROUP_A in groups
        assert groups[GROUP_A]["title"] == f"群組 {GROUP_A}"  # 沒有 worker 就拿不到真名
        assert groups[GROUP_A]["msg_count"] == 1


def test_groups_directory_refresh_triggers_read_only_discovery():
    client, db, manager, loop = _make_dashboard()
    with client:
        _login(client)
        worker = _setup_account(
            db, loop, manager, ACC_1,
            groups=[GROUP_A], worker_groups=[(GROUP_A, "桃花源・約會", 120)], selected=[GROUP_A],
        )
        assert worker.refresh_calls == 0
        client.get("/api/groups/directory?refresh=1")
        assert worker.refresh_calls == 1


def test_groups_directory_requires_login():
    client, db, manager, loop = _make_dashboard()
    with client:
        assert client.get("/api/groups/directory").status_code == 401


# ---------- 一鍵加入 / 移出 ----------


def test_membership_adds_and_removes_group_for_selected_accounts():
    client, db, manager, loop = _make_dashboard()
    with client:
        _login(client)
        _setup_account(
            db, loop, manager, ACC_1,
            groups=[GROUP_A], worker_groups=[(GROUP_A, "桃花源", 120), (GROUP_B, "測試群", 8)],
            selected=[GROUP_A],
        )
        _setup_account(
            db, loop, manager, ACC_2,
            groups=[GROUP_B], worker_groups=[(GROUP_B, "測試群", 8)], selected=[GROUP_B],
        )

        # 兩隻都加入 GROUP_B
        r = client.post(
            "/api/groups/membership",
            json={"group_id": GROUP_B, "account_ids": [ACC_1, ACC_2], "selected": True},
        )
        assert r.status_code == 200
        assert r.json()["results"] == {ACC_1: "ok", ACC_2: "ok"}
        assert manager.workers[ACC_1].selected_groups == {GROUP_A, GROUP_B}
        assert json_groups(db, loop, ACC_1) == [GROUP_A, GROUP_B]

        # 只把 ACC_2 移出
        r = client.post(
            "/api/groups/membership",
            json={"group_id": GROUP_B, "account_ids": [ACC_2], "selected": False},
        )
        assert r.status_code == 200
        # 移出後 ACC_2 已經沒有群 ⇒ 依既有規則停用該帳號
        assert "停用" in r.json()["results"][ACC_2]
        assert manager.workers[ACC_1].selected_groups == {GROUP_A, GROUP_B}


def test_membership_validates_payload():
    client, db, manager, loop = _make_dashboard()
    with client:
        assert client.post(
            "/api/groups/membership",
            json={"group_id": GROUP_A, "account_ids": [ACC_1], "selected": True},
        ).status_code == 401
        _login(client)
        assert client.post("/api/groups/membership", json={}).status_code == 400
        assert client.post(
            "/api/groups/membership", json={"group_id": GROUP_A, "account_ids": []}
        ).status_code == 400
        # 不存在的帳號：回 200 但逐帳號錯誤訊息
        r = client.post(
            "/api/groups/membership",
            json={"group_id": GROUP_A, "account_ids": ["nope"], "selected": True},
        )
        assert r.status_code == 200
        assert r.json()["results"]["nope"] == "帳號不存在"


def json_groups(db, loop, account_id):
    import json

    acc = loop.run_until_complete(db.get_account(account_id))
    assert acc is not None
    return json.loads(acc["groups"] or "[]")
