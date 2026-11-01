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


def test_extract_motifs():
  worker = _test_worker()
  m1 = worker._extract_motifs("外面太陽那麼大，誰買杯冰手搖把姐姐餵飽")
  assert "手搖" in m1 or "冰手搖" in m1
  assert "餵飽" in m1

  m2 = worker._extract_motifs("我剛噴好香水，吃泡麵哪有親我香嗎")
  assert "香水" in m2
  assert "泡麵" in m2

  m3 = worker._extract_motifs("剛洗完澡換好細肩帶在吹冷氣")
  assert "細肩帶" in m3
  assert "冷氣" in m3 or "吹冷氣" in m3


def test_director_note_suppresses_consecutive_questions():
  worker = _test_worker()
  event = SimpleNamespace(sender_id=999, chat_id=-1001)

  # 歷史最後一則是反問句
  history_with_q = [
      {"role": "user", "content": "今晚有空嗎"},
      {"role": "assistant", "content": "吃泡麵哪有親我香嗎？"},
  ]
  note = worker._build_directors_note(event, history_with_q)
  assert "嚴禁連續反問" in note
  assert "絕對禁止" in note


def test_ordinary_reply_spacing_cooldown():
  import time

  worker = _test_worker()
  worker.tg_user_id = 123
  event = SimpleNamespace(
      chat_id=-1001,
      id=10,
      sender_id=999,
      is_reply=False,
      mentioned=False,
      raw_text="今天好熱喔",
  )

  # 模擬 30 秒前群內已有水軍發言（90 秒保護內）
  worker.db.group_bot_last_spoke = AsyncMock(
      return_value={456: time.time() - 30.0}
  )

  async def main():
    allowed = await worker._ordinary_reply_allowed(event)
    assert allowed is False, "90 秒內已有水軍發言，普通訊息應靜默讓位"

    # 超過 95 秒，應放行
    worker.db.group_bot_last_spoke = AsyncMock(
        return_value={456: time.time() - 95.0}
    )
    worker.db.get_runtime_setting = AsyncMock(return_value="1.0")
    allowed_later = await worker._ordinary_reply_allowed(event)
    assert allowed_later is True, "超過 90 秒後普通訊息應放行"

  asyncio.run(main())

