"""網關欠費（402）：要跟「模型不存在」分開處理，也要看得見。

20:14 實測：routeway 餘額不足，四個帳號、含全部備援模型一起 402，群裡真人
35 則/10 分鐘、水軍零輸出 47 分鐘，/health 全程綠燈。換模型救不了同一條網關，
每一次呼叫還先燒一輪 timeout，所以必須記成網關級狀態並讓 /health 轉 degraded。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from fastapi.testclient import TestClient

from app.worker import AccountWorker


def _worker() -> AccountWorker:
    config = SimpleNamespace(
        ai_model="m",
        ai_base_url="https://api.routeway.ai/v1",
        ai_temperature=0.8,
        ai_max_tokens=200,
        ai_timeout=17,
        ai_disable_thinking=True,
        memory_max_messages=10,
        min_typing_delay=0,
        max_typing_delay=0,
        media_enabled=False,
        media_max_input_bytes=1024,
        decision_enabled=False,
    )
    return AccountWorker(
        account_id="w1",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=cast(Any, SimpleNamespace()),
        config=config,
        managed_ids=set(),
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[],
    )


class _Boom(Exception):
    pass


def _raise(exc):
    raise exc


def test_quota_error_marks_model_dead_and_stamps_gateway():
    w = _worker()
    w.ai_client = cast(
        Any,
        SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kw: _raise(
                        _Boom(
                            "Error code: 402 - {'error': {'message': "
                            "'Insufficient funds. Please add credits to continue.'}}"
                        )
                    )
                )
            )
        ),
    )
    import asyncio

    out = asyncio.run(w._call_ai("sys", "hi", model="gemini-4-argon"))
    assert out == ""
    # 欠費一次就當它死（別再排這顆），並留下網關級時間戳
    assert w._model_alive("gemini-4-argon") is False
    assert w.stats.get("gateway_quota_dead", 0) > 0
    assert abs(time.time() - w.stats["gateway_quota_dead"]) < 5


def test_health_degrades_on_recent_quota_death():
    import asyncio

    from app.config import load_settings
    from app.dashboard import Dashboard

    w = _worker()
    w.is_running = True
    w.reply_enabled = True
    w.stats["gateway_quota_dead"] = time.time()

    async def _last_at():
        return 0.0

    manager = SimpleNamespace(
        workers={"w1": w},
        db=SimpleNamespace(
            _c=SimpleNamespace(execute=lambda *a, **k: _async_ctx()),
            ai_call_summary=None,
            last_assistant_message_at=_last_at,
        ),
    )
    dash = Dashboard(load_settings(), manager, SimpleNamespace(prune_expired=None))
    with TestClient(dash.app) as client:
        payload = client.get("/health").json()
    assert payload["status"] == "degraded"
    assert "gateway_quota_dead_seconds" in payload


# --- 小工具 ---------------------------------------------------------------


class _async_ctx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def fetchone(self):
        return (1,)


async def _awaitable(v):
    return v


def test_client_smoke():
    """Dashboard 建得起來（路由註冊沒被改壞）。"""
    from app.config import load_settings
    from app.dashboard import Dashboard

    dash = Dashboard(load_settings(), SimpleNamespace(workers={}), SimpleNamespace())
    assert isinstance(TestClient(dash.app), TestClient)
