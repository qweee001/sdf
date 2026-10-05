"""決策層（System One）：① 怎麼回 → ② 照決策生成 → ③ 審核候選。"""
import asyncio
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from app import decision as decision_mod
from app import worker as worker_mod
from app.decision import DecisionError, system_one
from app.worker import AccountWorker


class _FakeDB:
    def __init__(self):
        self.messages = [
            {"sender_id": 11, "sender_name": "阿宏", "content": "剛吃完飯"},
            {"sender_id": 12, "sender_name": "美玲", "content": "有人去散步嗎"},
        ]
        self.shared = ["昨天有人說要去夜市"]
        self.group_msgs = [
            {"sender_id": 11, "sender_name": "阿宏", "content": "剛吃完飯", "role": "user"},
            {"sender_id": 12, "sender_name": "美玲", "content": "有人去散步嗎", "role": "user"},
        ]

    async def get_recent_messages(self, *a, **k):
        return list(self.messages)

    async def get_group_shared_notes(self, *a, **k):
        return list(self.shared)

    async def get_group_member_notes(self, *a, **k):
        return []

    async def get_recent_group_replies(self, *a, **k):
        return []

    async def get_group_messages(self, *a, **k):
        return list(self.group_msgs)


def _worker(decision_key: str = "") -> AccountWorker:
    config = SimpleNamespace(
        ai_model="test-model",
        ai_temperature=0.8,
        ai_max_tokens=200,
        ai_timeout=17,
        ai_disable_thinking=True,
        memory_max_messages=10,
        min_typing_delay=0,
        max_typing_delay=0,
        media_enabled=True,
        media_max_input_bytes=8 * 1024 * 1024,
        decision_api_key=decision_key,
        decision_base_url="https://api.typesafe.ai",
        decision_model="jev-latest",
        decision_timeout_seconds=3.0,
        decision_gate_threshold=0.5,
    )
    w = AccountWorker(
        account_id="w1",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=_FakeDB(),
        config=config,
        managed_ids=set(),
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[-1001],
    )
    w.persona = {
        "name": "小小",
        "age": 28,
        "city": "台北",
        "district": "士林",
        "gender": "女",
        "industry": "金融業",
        "university": "高師大",
        "personality": "俏皮、會接梗",
        "hobbies": ["吃火鍋", "看劇"],
        "looking_for": "想找對象",
        "meetups_done": 3,
        "chat_style": "俏皮少量表情",
    }
    return w


def _event():
    return SimpleNamespace(
        chat_id=-1001,
        id=1,
        sender_id=11,
        sender=SimpleNamespace(username="ahong"),
        raw_text="剛吃完飯",
        mentioned=False,
        is_reply=False,
        reply_to=None,
        media=None,
    )


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, timeout):
        self.timeout = timeout
        self.url = None
        self.body = None
        self.headers = None
        self.resp = _FakeResp(200, {"answers": {}})
        self.raise_on_enter = None

    async def __aenter__(self):
        if self.raise_on_enter:
            raise self.raise_on_enter
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.url = url
        self.body = json
        self.headers = headers
        return self.resp


def _patch_client(monkeypatch, client: _FakeClient):
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=None: client)


# ---------------------------------------------------------------- system_one

def test_system_one_parses_answers(monkeypatch):
    client = _FakeClient(2.5)
    client.resp = _FakeResp(200, {"answers": {"q": {"type": "noul", "noul": 0.9}}})
    _patch_client(monkeypatch, client)
    answers = asyncio.run(
        system_one(
            "state",
            {"q": {"type": "noul"}},
            base_url="https://api.typesafe.ai/",
            api_key="k",
            model="jev",
            timeout_seconds=2.5,
        )
    )
    assert answers == {"q": {"type": "noul", "noul": 0.9}}
    assert client.url == "https://api.typesafe.ai/v1/systemone"
    assert client.headers["Authorization"] == "Bearer k"
    assert client.body["model"] == "jev"


def test_system_one_http_error_raises(monkeypatch):
    client = _FakeClient(2.5)
    client.resp = _FakeResp(402, text="insufficient")
    _patch_client(monkeypatch, client)
    with pytest.raises(DecisionError):
        asyncio.run(
            system_one(
                "s", {}, base_url="https://x", api_key="k", model="m", timeout_seconds=1
            )
        )


def test_system_one_timeout_raises(monkeypatch):
    client = _FakeClient(2.5)
    client.raise_on_enter = httpx.TimeoutException("slow")
    _patch_client(monkeypatch, client)
    with pytest.raises(DecisionError):
        asyncio.run(
            system_one(
                "s", {}, base_url="https://x", api_key="k", model="m", timeout_seconds=1
            )
        )


# ---------------------------------------------------------------- ① decide

def test_decide_action_reply_flirty_rounds(monkeypatch):
    w = _worker("k")
    async def fake(state, questions, **kw):
        assert "最新消息" in state
        return {
            "action": {"choice": "reply", "probabilities": {"reply": 0.9}},
            "flirty": {"score": 1.4},
        }
    monkeypatch.setattr(worker_mod, "system_one", fake)
    assert asyncio.run(w._decide_action(_event())) == {"action": "reply", "flirty": 1}
    assert w.stats.get("decision_calls") == 1

    # 頂檔無底線：score 3.x 圓整到 3 不被截掉
    async def fake_top(state, questions, **kw):
        return {"action": {"choice": "reply"}, "flirty": {"score": 3.4}}
    monkeypatch.setattr(worker_mod, "system_one", fake_top)
    assert asyncio.run(w._decide_action(_event())) == {"action": "reply", "flirty": 3}


def test_decide_action_returns_none_on_error(monkeypatch):
    w = _worker("k")
    async def fake(state, questions, **kw):
        raise DecisionError("http 500")
    monkeypatch.setattr(worker_mod, "system_one", fake)
    assert asyncio.run(w._decide_action(_event())) is None
    assert w.stats.get("decision_errors") == 1


def test_decide_action_disabled_returns_none():
    w = _worker("")
    assert asyncio.run(w._decide_action(_event())) is None


def test_apply_decision_routes(monkeypatch):
    w = _worker("k")
    # skip
    async def dec(state, questions, **kw):
        return {"action": {"choice": "skip"}, "flirty": {"score": 0}}
    monkeypatch.setattr(worker_mod, "system_one", dec)
    route, payload = asyncio.run(w._apply_decision(_event(), forced_text=False))
    assert route == "skip"
    # forced_text（被@）覆蓋 skip → 照樣回話
    route, payload = asyncio.run(w._apply_decision(_event(), forced_text=True))
    assert route == "reply" and payload["action"] == "skip"
    # react 發送成功
    async def dec2(state, questions, **kw):
        return {"action": {"choice": "react"}, "flirty": {"score": 2.0}}
    monkeypatch.setattr(worker_mod, "system_one", dec2)

    async def _react(event):
        return True
    w._send_group_reaction = _react
    route, ok = asyncio.run(w._apply_decision(_event(), forced_text=False))
    assert route == "react" and ok is True


# ---------------------------------------------------------------- ② generate

def test_generate_reply_injects_flirty_guide(monkeypatch):
    w = _worker("k")
    captured = {}

    async def fake_call(system_prompt, message, **kw):
        captured["message"] = message
        return "好哦～一起去嘛"
    w._call_ai = lambda *a, **kw: fake_call(*a, **kw)

    ev = _event()
    ev._sdf_decision = {"action": "reply", "flirty": 2}
    out = asyncio.run(w._generate_reply(ev))
    assert out == "好哦～一起去嘛"
    assert "這次互動決策" in captured["message"]
    assert "明顯勾引，露骨主動" in captured["message"]

    # 頂檔：無底線指示進 prompt
    ev3 = _event()
    ev3._sdf_decision = {"action": "reply", "flirty": 3}
    asyncio.run(w._generate_reply(ev3))
    assert "尺度完全放開，無底線" in captured["message"]

    # 沒有決策 → 不注入
    ev2 = _event()

    async def fake_call2(system_prompt, message, **kw):
        captured["message"] = message
        return "好哦～一起去嘛"
    w._call_ai = lambda *a, **kw: fake_call2(*a, **kw)
    asyncio.run(w._generate_reply(ev2))
    assert "這次互動決策" not in captured["message"]


def test_generate_reply_extra_hint(monkeypatch):
    w = _worker("k")
    captured = {}

    async def fake_call(system_prompt, message, **kw):
        captured["message"] = message
        return "好哦～一起去嘛"
    w._call_ai = lambda *a, **kw: fake_call(*a, **kw)
    asyncio.run(w._generate_reply(_event(), extra_hint="上一版被決策層攔下。"))
    assert "上一版被決策層攔下。" in captured["message"]


# ---------------------------------------------------------------- ③ gate

def test_gate_reply_pass(monkeypatch):
    w = _worker("k")
    async def fake(state, questions, **kw):
        assert "你要發出的回覆" in state
        return {
            "sendable": {"noul": 0.9},
            "issue": {"choice": "none"},
        }
    monkeypatch.setattr(worker_mod, "system_one", fake)
    out = asyncio.run(w._gate_reply(_event(), "今天好熱喔"))
    assert out == "今天好熱喔"
    assert w.stats.get("gate_pass") == 1


def test_gate_rewrite_then_pass(monkeypatch):
    w = _worker("k")
    calls = {"review": 0}

    async def fake(state, questions, **kw):
        calls["review"] += 1
        if calls["review"] == 1:
            return {"sendable": {"noul": 0.2}, "issue": {"choice": "time"}}
        return {"sendable": {"noul": 0.9}, "issue": {"choice": "none"}}
    monkeypatch.setattr(worker_mod, "system_one", fake)

    async def fake_gen(event, *, extra_hint=""):
        calls.setdefault("extra_hint", extra_hint)
        return "晚上去散步嗎"
    w._generate_reply = lambda event, **kw: fake_gen(event, **kw)

    out = asyncio.run(w._gate_reply(_event(), "早安呀～早餐吃什麼"))
    assert out == "晚上去散步嗎"
    assert w.stats.get("gate_rewrite") == 1
    assert w.stats.get("gate_pass_after_rewrite") == 1
    assert "時段穿幫" in calls["extra_hint"]


def test_gate_held_when_rewrite_still_fails(monkeypatch):
    w = _worker("k")
    async def fake(state, questions, **kw):
        return {"sendable": {"noul": 0.2}, "issue": {"choice": "time"}}
    monkeypatch.setattr(worker_mod, "system_one", fake)

    async def fake_gen(event, *, extra_hint=""):
        return "早安餓死了"
    w._generate_reply = lambda event, **kw: fake_gen(event, **kw)
    assert asyncio.run(w._gate_reply(_event(), "早安")) == ""
    assert w.stats.get("gate_held") == 1
    assert w.stats.get("gate_rewrite") == 1


def test_gate_held_on_timeout(monkeypatch):
    w = _worker("k")
    async def fake(state, questions, **kw):
        raise DecisionError("timeout")
    monkeypatch.setattr(worker_mod, "system_one", fake)
    assert asyncio.run(w._gate_reply(_event(), "今天好熱喔")) == ""
    assert w.stats.get("gate_held") == 1


def test_gate_disabled_passthrough():
    w = _worker("")
    out = asyncio.run(w._gate_reply(_event(), "今天好熱喔"))
    assert out == "今天好熱喔"
    assert not w.stats.get("gate_pass")


def test_review_threshold():
    w = _worker("k")
    assert w._review_passes({"sendable": 0.5, "issue": "none"}) is True
    assert w._review_passes({"sendable": 0.49, "issue": "none"}) is False
    assert w._review_passes({"sendable": 0.9, "issue": "time"}) is False
