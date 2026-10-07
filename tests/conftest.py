import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 動態產生合法 Fernet 金鑰（避免硬編碼憑證被遮蔽）
try:
    from cryptography.fernet import Fernet

    _KEY = Fernet.generate_key().decode()
except Exception:
    _KEY = "dGVzdF9rZXlfdGVzdF9rZXlfdGVzdF9rZXlfdGVzdF9rZX"

os.environ.setdefault("TG_API_ID", "12345678")
os.environ.setdefault("TG_API_HASH", "test_hash")
os.environ.setdefault("ACCOUNT_ENCRYPTION_KEY", _KEY)
os.environ.setdefault("DASHBOARD_USER", "admin")
os.environ.setdefault("DASHBOARD_PASS", "secret123")
os.environ.setdefault("DB_PATH", "/tmp/sdf_test/chat.db")
os.environ.setdefault("AI_MODEL", "test-model")
os.environ.setdefault("AI_BASE_URL", "https://api.test/v1")
os.environ.setdefault("AI_API_KEY", "***")


@pytest.fixture(autouse=True)
def _deterministic_human_touch(monkeypatch):
    """人味隨機門（reaction／沉默）預設關閉，讓既有確定性測試不受抽樣影響。

    專測這些門的測試直接調用 _pick_reaction／_send_group_reaction，
    或自己重新設回常數即可。
    """
    from app import worker as worker_mod

    monkeypatch.setattr(worker_mod, "_REACTION_PROBABILITY", 0.0)
    monkeypatch.setattr(
        worker_mod, "_SILENT_REPLY_PROBABILITY_DIRECTED", 0.0
    )
    monkeypatch.setattr(
        worker_mod, "_SILENT_REPLY_PROBABILITY_ORDINARY", 0.0
    )
    # 睡窗門關掉（sleeping 也照回），避免測試被台北凌晨 4-7 點的真实時鐘影響
    monkeypatch.setattr(worker_mod, "_SLEEP_REPLY_PROBABILITY", 1.0)


_EXIT = {"code": 0}


def pytest_sessionfinish(session, exitstatus):
    """記錄真實測試結果碼"""
    _EXIT["code"] = int(exitstatus or 0)


def _drain_database_connections():
    """關閉所有還在跑的 aiosqlite 連線，讓其背景線程結束。

    aiosqlite 每個連線會起一條 background 線程（非 daemon）。測試裡大量
    `asyncio.run(main())` 各自建一個新 loop，loop 收場時不會主動 drain
    aiosqlite 的 pending 工作，那些連線的背景線程就殘留，讓 process 正常
    退出卡住。這裡在 pytest 收場時把仍活著（`_running` 為真）的連線統一
    關閉。aiosqlite 沒提供全域連線清單，用 gc 掃活著實例。
    """
    import gc

    import aiosqlite
    import asyncio

    connections = {
        obj
        for obj in gc.get_objects()
        if isinstance(obj, aiosqlite.Connection) and getattr(obj, "_running", False)
    }
    for conn in connections:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(conn.close())
        except Exception:
            pass
        finally:
            loop.close()


def pytest_unconfigure(config):
    """測試結束後收場：關閉殘留 aiosqlite 連線，讓 process 正常退出。

    F14：原本直接用 os._exit 兜底，會把 pytest 標準結果總結（passed/failed 行）
    一起吞掉——CI 日誌有完整進度但沒有結果行，判讀全靠外掛。改成真正關閉
    殘留連線後走正常退出，結果總結才會印出來。
    """
    _drain_database_connections()
