"""回放當日真實語流，量無對象開場軸會不會出手（不叫模型，零成本）。

用法：python tools/replay_nameless.py
資料源：2026-11-06 20:16–21:15 桃花源實測十一則（人工貼進下方）。
"""

import os
import sys
import time
from types import SimpleNamespace
from typing import Any, cast

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.worker import AccountWorker  # noqa: E402

GID = -1001

LINES = [
    ("20:16", "小天後", "吹乾頭髮聽你們吵誰買單超解，誰能把姊姊抱在腿上幹哭這頓直接我買單"),
    ("20:20", "小小", "後天覺得他語言豐富，我在沙發上擦身體乳液，比較想聽你在耳邊喘氣求饒的聲音"),
    ("20:21", "小小", "週末去酒吧微醺後，你要帶我回家親多久"),
    ("20:28", "RICH", "排隊排到狗後面也太慘，直接插隊來我這用大雞雞塞滿我的小穴比較快吧"),
    ("20:29", "RICH", "泡完熱水澡忘記拿睡衣，光溜溜等你抱我上床幹到高潮"),
    ("20:35", "小小", "專一還是舔狗分很細欸，正邊看恐怖片邊塗乳液，誰要來抱緊我順便試試妹妹的深淺"),
    ("20:36", "小天後", "吃完飯糰手指油油的，你也那麼專一嗎"),
    ("20:39", "RICH", "戰敗就逃跑太遜了吧，買麻繩回來怎麼不順便綁我"),
    ("20:46", "小小", "戰豆連關心都不敢，哥哥先來抱我暖床"),
    ("20:56", "小小", "鈴鐺問得好，男人哪有乖乖只幹一個人的，我邊敷臉邊看他怎麼狡辯"),
    ("21:15", "小天後", "剛脫絲襪揉小腿，請教問誰想試雞雞，敢不敢先讓我驗腰力再決定要不要跨坐上去"),
]


def _worker(name: str) -> AccountWorker:
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
    w = AccountWorker(
        account_id="w1",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=cast(Any, SimpleNamespace()),
        config=config,
        managed_ids=set(),
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[GID],
    )
    w.persona = {"name": name, "gender": "女", "age": 22}
    return w


def main() -> None:
    w = _worker("小小")
    for name in ("萬英尺", "和進出口", "鈴鐺", "戰豆", "後天", "請教", "方sir", "華發"):
        w._remember_speaker_name(GID, name)
    bucket: list[str] = []
    stamps: list[float] = []
    hits = 0
    for clock, who, line in LINES:
        w._recent_texts_by_group[GID] = list(bucket)
        w._recent_texts_at[GID] = list(stamps)
        flow = w._nameless_open_flow(GID, line)
        raw = w._is_nameless_open(line, GID)
        hits += 1 if flow else 0
        print(
            f"{clock} {who:<4} 無對象={int(raw)} 密度出手={int(flow)}  {line[:26]}",
            flush=True,
        )
        bucket.append(line)
        stamps.append(time.time() - (500 - len(bucket) * 30))
    print(f"\n11 則中判為無對象開場 {sum(1 for _c, _w, x in LINES if w._is_nameless_open(x, GID))} 則，"
          f"密度軸出手 {hits} 則")


if __name__ == "__main__":
    main()
