"""跨帳號句子記憶必須真的跨得過帳號邊界（2026-11-03 20:12~21:01 實測破口）。

- 20:12:41 小小「華哥只會出一張嘴…」→ 20:20:13 Chen「…難怪只能出一張嘴」（8 分鐘同一個慣用語）
- 21:01:50 小天後又來一次「笑死，只會出一張嘴真的很解」

根因：_recent_texts_by_group 是 worker 實例屬性，四號各記各的，_local_veto 的
「別人剛用過」永遠只看得到自己 → 跨號去重整套形同關閉。修法照 phrase_cores_by_group
的模式由 manager 注入同一份 dict，這裡直接驗注入與否的行為差異。
"""

from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001
EARLIER = "華哥只會出一張嘴，真讓你上你撐不到十分鐘"
LATER = "舔這麼久是在品嚐還是在找東西啊，技術這麼差難怪只能出一張嘴"


def _worker(name: str, *, texts: dict | None = None, stamps: dict | None = None) -> AccountWorker:
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
        decision_enabled=False,
    )
    kwargs: dict[str, Any] = {}
    if texts is not None:
        kwargs["recent_texts_by_group"] = texts
    if stamps is not None:
        kwargs["recent_texts_at"] = stamps
    w = AccountWorker(
        account_id=f"acc-{name}",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=cast(Any, SimpleNamespace()),
        config=config,
        managed_ids=set(),
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[GID],
        **kwargs,
    )
    w.persona = {"name": name, "gender": "女", "age": 30}
    return w


def test_injected_shared_bucket_makes_cross_account_veto_fire():
    texts: dict = {}
    stamps: dict = {}
    xiao = _worker("小小", texts=texts, stamps=stamps)
    chen = _worker("Chen", texts=texts, stamps=stamps)
    xiao._record_sent_text(GID, EARLIER)
    veto = chen._local_veto(LATER, [], "", GID)
    assert veto, "別號 8 分鐘前的慣用語沒被跨號看到"
    assert "張嘴" in veto


def test_without_injection_each_worker_only_sees_itself():
    """回歸對照：不注入時（舊行為）同一個案例抓不到，證明修的是共用而非門檻。"""
    xiao = _worker("小小")
    chen = _worker("Chen")
    xiao._record_sent_text(GID, EARLIER)
    assert chen._local_veto(LATER, [], "", GID) == ""


def test_shared_bucket_is_the_same_object_across_workers():
    texts: dict = {}
    a = _worker("小小", texts=texts)
    b = _worker("RICH", texts=texts)
    a._record_sent_text(GID, EARLIER)
    assert b._recent_texts_by_group is a._recent_texts_by_group
    assert b._recent_texts_by_group[GID] == [EARLIER]
