"""A 階段止血的契約測試（外部審查 P0/P1）。

這批修的是「測試替身太寬鬆所以看不出來」的缺陷：
- 貼圖用 send_message(media=…) → 固定版 Telethon 沒這個參數，執行期必炸
- 審核上下文取不到時直接放行候選文字（fail-open）
- 每日主動額度只存在記憶體，容器重啟就歸零
- 控制台首次登入沒綁帳號卡按鈕（要手動重整才能按）
"""

import asyncio
import inspect
import time

from test_worker_reply_arbitration import _worker


def test_sticker_send_uses_supported_rpc():
    """真 Telethon 的 send_message 沒有 media 參數；我們必須走 SendMediaRequest。"""

    async def main():
        import telethon
        from telethon.tl.functions.messages import SendMediaRequest

        sig = inspect.signature(telethon.TelegramClient.send_message)
        assert "media" not in sig.parameters, "Telethon 改了簽名，本測試要重新評估"
        # send_message 只收 file=（內部轉 send_file），沒有 media=；貼圖因此必須走 TL 原語
        assert "file" in sig.parameters

        source = inspect.getsource(
            __import__("app.worker", fromlist=["x"]).AccountWorker._send_group_sticker
        )
        assert "send_message(" not in source, "貼圖不能再走 send_message"
        assert "SendMediaRequest(" in source
        assert SendMediaRequest is not None

    asyncio.run(main())


def test_gate_holds_when_context_unavailable(monkeypatch):
    """上下文組不出來時要暫緩，不能放行未審核文字（原本 fail-open）。"""

    async def main():
        worker = _worker(101)
        worker.config.decision_api_key = "k"

        async def boom(_event):
            raise RuntimeError("db down")

        worker._decision_state = boom
        out = await worker._gate_reply(None, "這句沒被審核過")
        assert out == ""
        assert worker.stats.get("gate_held") == 1

    asyncio.run(main())


def test_proactive_quota_survives_restart():
    """同一天重啟：額度要從 DB 讀回來，不能歸零。"""

    async def main():
        worker = _worker(101)
        today = worker._today_index()
        store = {f"proactive_today:{worker.account_id}": f"{today}:5"}

        async def get_settings():
            return dict(store)

        async def set_settings(values):
            store.update(values)

        worker.db.get_runtime_settings = get_settings
        worker.db.set_runtime_settings = set_settings
        worker._reset_proactive_day()
        assert worker._proactive_today == 0

        # 模擬重啟後第一次同步
        worker._proactive_day = -1
        worker._proactive_today = 0
        await worker._restore_proactive_quota()
        assert worker._proactive_today == 5

        # 發一條之後要寫回 DB
        worker._proactive_today += 1
        await worker._persist_proactive_quota()
        assert store[f"proactive_today:{worker.account_id}"] == f"{today}:6"

    asyncio.run(main())


def test_proactive_quota_ignores_stale_day():
    """DB 裡是昨天的額度時不能沿用。"""

    async def main():
        worker = _worker(101)
        today = worker._today_index()
        store = {f"proactive_today:{worker.account_id}": f"{today - 1}:5"}

        async def get_settings():
            return dict(store)

        worker.db.get_runtime_settings = get_settings
        worker._proactive_day = -1
        worker._proactive_today = 0
        await worker._restore_proactive_quota()
        assert worker._proactive_today == 0

    asyncio.run(main())


def test_dashboard_binds_account_actions_after_login():
    """首次登入要補綁事件，否則新開頁面後所有按鈕都沒反應。"""

    async def main():
        from app.dashboard import PAGE

        assert "function bindAccountActions()" in PAGE
        assert PAGE.count("bindAccountActions()") >= 2, "登入成功後也要呼叫一次"
        # 綁定旗標避免重複疊加
        assert "dataset.bound" in PAGE
        # 登入流程裡要把補綁放在顯示主畫面之後
        login_block = PAGE.split("async function doLogin()", 1)[1].split("async function doLogout()", 1)[0]
        assert "bindAccountActions();" in login_block

    asyncio.run(main())


def test_dashboard_escapes_persona_fields():
    """人設是模型生成的，插進 innerHTML 前必須轉義。"""

    async def main():
        from app.dashboard import PAGE

        card_block = PAGE.split("document.getElementById('accounts').innerHTML", 1)[1].split("}).join('')", 1)[0]
        for field in ("persona.name", "persona.gender", "persona.district", "persona.industry"):
            assert f"esc({field}" in card_block, f"{field} 沒有 esc()"

    asyncio.run(main())


def test_dockerfile_runs_tests_before_producing_image():
    """發版閘門：測試不過就不該產出映像（外部審查抓到 13 次測試失敗仍部署）。

    三個必要條件，缺一就會出現「閘門看起來有、其實沒擋」：
      1) 建置流程裡真的有 pytest
      2) .dockerignore 不能把 tests 排除（否則 COPY 直接失敗或測試跑不到）
      3) 測試需要的東西（app／tools／Dockerfile 本身）都要先複製
    """

    async def main():
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        ignored = (root / ".dockerignore").read_text(encoding="utf-8").split()

        assert "python -m pytest" in dockerfile
        assert "tests" not in ignored, ".dockerignore 排除 tests 會讓閘門失效"
        # 測試會 import app、讀 tools/ 與 Dockerfile，複製順序必須在 pytest 之前
        gate_at = dockerfile.index("python -m pytest")
        for needed in ("COPY app ./app", "COPY tests ./tests", "COPY tools ./tools", "COPY Dockerfile ./Dockerfile"):
            assert needed in dockerfile, f"閘門缺了 {needed}"
            assert dockerfile.index(needed) < gate_at, f"{needed} 必須在 pytest 之前"
        assert "test_video_render_runs_in_own_task_without_blocking_text_or_voice" in dockerfile

    asyncio.run(main())
