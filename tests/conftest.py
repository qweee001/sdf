import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 動態產生合法 Fernet 金鑰（避免硬編碼憑證被遮蔽）
try:
    from cryptography.fernet import Fernet

    _KEY = Fernet.generate_key().decode()
except Exception:
    _KEY = "dGVzdF9rZXlfdGVzdF9rZXlfdGVzdF9rZXlfdGVzdF9rZX"

os.environ.setdefault("TG_API_ID", "30279608")
os.environ.setdefault("TG_API_HASH", "test_hash")
os.environ.setdefault("ACCOUNT_ENCRYPTION_KEY", _KEY)
os.environ.setdefault("DASHBOARD_USER", "admin")
os.environ.setdefault("DASHBOARD_PASS", "secret123")
os.environ.setdefault("DB_PATH", "/tmp/sdf_test/chat.db")
os.environ.setdefault("AI_MODEL", "test-model")
os.environ.setdefault("AI_BASE_URL", "https://api.test/v1")
os.environ.setdefault("AI_API_KEY", "***")


_EXIT = {"code": 0}


def pytest_sessionfinish(session, exitstatus):
    """記錄真實測試結果碼"""
    _EXIT["code"] = int(exitstatus or 0)


def pytest_unconfigure(config):
    """測試結束後強制收場，避免 aiosqlite 背景線程卡住 process 退出。

    生產環境只用一個 Database 一個 event loop，不會有這個問題；
    這裡每個 dashboard 測試各建一個 aiosqlite 連線（不同 loop），
    這些背景線程不會在正常退出時 drain。帶上真實 pytest 結果碼收場。
    """
    import os

    os._exit(_EXIT["code"])
