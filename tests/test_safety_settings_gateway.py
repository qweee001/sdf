"""網關相容：Gemini 的 safety_settings 只有部分網關吃。

experientiallabs 的 gateway profile 對 safety_settings 直接 400
（"not supported by this gateway profile"），一送整條呼叫就失敗——審核層會
一路退回「不確定→暫緩」，外層還看不出原因。routeway 吃這組參數。
判別走 base_url，不寫死模型名：同一顆模型換網關結果就不同。
"""

from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker


def _worker(base_url: str) -> AccountWorker:
    config = SimpleNamespace(
        ai_model="m",
        ai_base_url=base_url,
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


def test_routeway_accepts_safety_settings():
    assert _worker("https://api.routeway.ai/v1")._supports_safety_settings() is True


def test_experientiallabs_does_not_accept_safety_settings():
    assert (
        _worker("https://api.experientiallabs.ai/v1")._supports_safety_settings()
        is False
    )
