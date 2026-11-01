import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.worker import AccountWorker


def _test_worker(ai_client=None, decision_api_key=""):
  cfg = SimpleNamespace(
      decision_api_key=decision_api_key,
      decision_base_url="https://api.typesafe.ai",
      decision_model="jev-latest",
      decision_timeout_seconds=3.0,
      decision_gate_threshold=0.5,
      vision_model="gemini-3.5-flash-lite",
      ai_model="gemini-4-argon",
      ai_fallback_models=(),
      memory_max_messages=30,
  )
  db = AsyncMock()
  w = AccountWorker(
      account_id="test_acc",
      session_key="test_session",
      tg_api_id=123,
      tg_api_hash="hash",
      ai_client=ai_client,
      db=db,
      config=cfg,
      managed_ids={123},
      on_status_change=None,
  )
  w.persona = {
      "name": "Chen",
      "gender": "女",
      "age": 38,
      "personality": "成熟性感、服飾店老闆娘",
  }
  return w


def test_decision_enabled_with_ai_client():
  w = _test_worker(ai_client=AsyncMock(), decision_api_key="")
  assert w._decision_enabled() is True

  w_no_ai = _test_worker(ai_client=None, decision_api_key="")
  assert w_no_ai._decision_enabled() is False

  w_jev = _test_worker(ai_client=None, decision_api_key="sk-test")
  assert w_jev._decision_enabled() is True


def test_gateway_reviewer_catches_ai_leak():
  ai_client = AsyncMock()
  create_mock = AsyncMock(
      return_value=SimpleNamespace(
          choices=[
              SimpleNamespace(
                  message=SimpleNamespace(
                      content=json.dumps({
                          "sendable": 0.0,
                          "issue": "ai_leak",
                          "reason": "英文系統任務狀態詞穿幫",
                      })
                  )
              )
          ],
          usage=SimpleNamespace(prompt_tokens=20, completion_tokens=15),
      )
  )
  ai_client.chat = SimpleNamespace(
      completions=SimpleNamespace(create=create_mock)
  )

  worker = _test_worker(ai_client=ai_client, decision_api_key="")

  async def main():
    review = await worker._review_candidate(
        context="群友都在聊吃的", text="task ready"
    )
    assert review is not None
    assert review["sendable"] == 0.0
    assert review["issue"] == "ai_leak"
    assert worker._review_passes(review) is False

  asyncio.run(main())


def test_gateway_reviewer_passes_natural_reply():
  ai_client = AsyncMock()
  create_mock = AsyncMock(
      return_value=SimpleNamespace(
          choices=[
              SimpleNamespace(
                  message=SimpleNamespace(
                      content=json.dumps({
                          "sendable": 0.95,
                          "issue": "none",
                          "reason": "自然道地的台灣熟女碎念調情",
                      })
                  )
              )
          ],
          usage=SimpleNamespace(prompt_tokens=25, completion_tokens=18),
      )
  )
  ai_client.chat = SimpleNamespace(
      completions=SimpleNamespace(create=create_mock)
  )

  worker = _test_worker(ai_client=ai_client, decision_api_key="")

  async def main():
    review = await worker._review_candidate(
        context="群友都在聊吃的", text="晚上吃什麼啊，姊姊剛收完店好餓喔🤤"
    )
    assert review is not None
    assert review["sendable"] >= 0.9
    assert review["issue"] == "none"
    assert worker._review_passes(review) is True

  asyncio.run(main())
