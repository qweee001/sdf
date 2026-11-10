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


def test_gateway_reviewer_catches_mainland_slang_dynamically():
  ai_client = AsyncMock()
  create_mock = AsyncMock(
      return_value=SimpleNamespace(
          choices=[
              SimpleNamespace(
                  message=SimpleNamespace(
                      content=json.dumps({
                          "sendable": 0.0,
                          "issue": "unnatural",
                          "reason": "包含大陸網路詞『白嫖』，台灣在地口語應為『被當盤子』『白睡』『白吃白喝』",
                      })
                  )
              )
          ],
          usage=SimpleNamespace(prompt_tokens=30, completion_tokens=25),
      )
  )
  ai_client.chat = SimpleNamespace(
      completions=SimpleNamespace(create=create_mock)
  )

  worker = _test_worker(ai_client=ai_client, decision_api_key="")

  async def main():
    review = await worker._review_candidate(
        context="群友都在聊約會買單",
        text="養一個月換一頓飯買單，被白嫖成這樣還有臉生氣喔，真沒用",
    )
    assert review is not None
    assert review["sendable"] == 0.0
    assert review["issue"] == "unnatural"
    assert "白嫖" in review["reason"]
    assert worker._review_passes(review) is False

  asyncio.run(main())


def test_group_attention_note_flags_pile_on():
  """四個女號輪流圍剿同一個男人 → 動態情境提示必須出現。"""
  w = _test_worker(ai_client=AsyncMock(), decision_api_key="")
  base = 1_700_000_000.0
  history = [
      {"role": "user", "sender_name": "萬英尺", "content": "我最猛", "timestamp": base},
      {"role": "user", "sender_name": "深情森森", "content": "是喔", "timestamp": base + 1},
      {"role": "user", "sender_name": "鈴鐺", "content": "哈哈", "timestamp": base + 2},
  ]
  bodies = [
      ("Chen", "萬哥口氣這麼大，等等見面連牽手都不敢吧"),
      ("RICH", "萬哥胃口這麼大吃得消嗎，小心腰先散掉"),
      ("小天後", "萬哥找不到你，那你躲來我被窩裡避難啊"),
      ("小小", "萬哥男女通吃這麼餓喔"),
  ]
  for i, (who, body) in enumerate(bodies):
      history.append({
          "role": "assistant", "sender_name": who, "content": body,
          "timestamp": base + 10 + i * 5,
      })

  note = w._group_attention_note(history)
  assert "萬英尺" in note
  assert "不要" in note
  # 沒被聊到的人要被點名出來當替代目標
  assert "深情森森" in note or "鈴鐺" in note


def test_group_attention_note_silent_when_balanced():
  w = _test_worker(ai_client=AsyncMock(), decision_api_key="")
  base = 1_700_000_000.0
  history = [
      {"role": "user", "sender_name": "萬英尺", "content": "hi", "timestamp": base},
      {"role": "user", "sender_name": "鈴鐺", "content": "hi", "timestamp": base + 1},
      {"role": "assistant", "sender_name": "Chen", "content": "萬哥早", "timestamp": base + 5},
      {"role": "assistant", "sender_name": "RICH", "content": "鈴鐺早", "timestamp": base + 15},
  ]
  assert w._group_attention_note(history) == ""


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
  # 佔比達標（3/10＝30%）→ 讓位秒數才回到 90 秒
  worker.db.group_traffic_window = AsyncMock(
      return_value=[
          {"role": "assistant" if i < 3 else "user", "sender_id": i} for i in range(10)
      ]
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



def test_hot_field_budget_scales_and_stale_override():
  """熱場額度隨真人密度放大；水軍潛水過久要能強制回場。

  實測 11-03 21:41→21:55 群裡 40 則男群友自聊、水軍 14 分半完全沒出聲。
  """
  import time

  worker = _test_worker()
  worker.tg_user_id = 123
  event = SimpleNamespace(
      chat_id=-1001,
      id=45,  # hash 落點 0.028，熱場 0.15 機率下穩定放行
      sender_id=999,
      is_reply=False,
      mentioned=False,
      raw_text="我18公分不含頭",
  )
  worker.db.get_runtime_setting = AsyncMock(return_value="1.0")

  # 45 則/5 分鐘的熱場，額度已經吃到 2（舊版固定 2 就死在這裡）
  worker.db.interaction_pressure = AsyncMock(
      return_value={
          "human_5m": 45,
          "human_sent_5m": 1,
          "ordinary_claimed_5m": 2,
          "ordinary_claimed_20s": 0,
          "ordinary_claimed_10m": 2,
      }
  )

  async def main():
    worker.db.group_bot_last_spoke = AsyncMock(
        return_value={456: time.time() - 120.0}
    )
    assert await worker._ordinary_reply_allowed(event) is True

    # 潛水 7 分鐘：額度滿、20 秒鎖踩著也要放行（回場）
    worker.db.interaction_pressure = AsyncMock(
        return_value={
            "human_5m": 45,
            "human_sent_5m": 1,
            "ordinary_claimed_5m": 4,
            "ordinary_claimed_20s": 1,
            "ordinary_claimed_10m": 4,
        }
    )
    worker.db.group_bot_last_spoke = AsyncMock(
        return_value={456: time.time() - 420.0}
    )
    assert await worker._ordinary_reply_allowed(event) is True

    # 只潛水 2 分鐘 → 額度滿就繼續讓路（佔比要先達標，5 分鐘額度才算數）
    worker.db.group_bot_last_spoke = AsyncMock(
        return_value={456: time.time() - 120.0}
    )
    worker.db.group_traffic_window = AsyncMock(
        return_value=[
            {"role": "assistant" if i < 15 else "user", "sender_id": i}
            for i in range(30)
        ]
    )
    assert await worker._ordinary_reply_allowed(event) is False

  asyncio.run(main())
