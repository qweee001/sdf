"""把真實水軍發言逐則回放 _local_veto，時間軸錨在候選當下（不是現在）。

用法：python tools/replay_gate.py <rec.json> <START_HH:MM> [--from-db]
輸出每則判定與總計；目的是量新規則多殺哪幾則、有沒有誤殺。
"""
import datetime
import json
import os
import sys
import time
import zoneinfo
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

PATH = sys.argv[1]
START = sys.argv[2] if len(sys.argv) > 2 else "00:00"
cfg = SimpleNamespace(
    ai_model="m", ai_temperature=0.8, ai_max_tokens=200, ai_timeout=17,
    ai_disable_thinking=True, memory_max_messages=10, min_typing_delay=0,
    max_typing_delay=0, media_enabled=True, media_max_input_bytes=1, decision_enabled=False,
)
w = AccountWorker(account_id="w", session_key="k", tg_api_id=1, tg_api_hash="h",
                  ai_client=cast(Any, None), db=cast(Any, SimpleNamespace()), config=cfg,
                  managed_ids=set(), on_status_change=lambda *a, **k: None, selected_groups=[-1001])
w.persona = {"name": "Chen", "gender": "女", "age": 30}

d = json.load(open(PATH, encoding="utf-8"))
tz = zoneinfo.ZoneInfo("Asia/Taipei")
rows = sorted(((datetime.datetime.fromtimestamp(r["timestamp"], tz), r) for r in d["records"]),
              key=lambda x: x[0])
_h, _m = (int(x) for x in START.split(":"))
b = rows[0][0]
start = b.replace(hour=_h, minute=_m, second=0)

gid = -1001
sent: list[tuple[datetime.datetime, str, str]] = []
w._recent_texts_by_group[gid] = []
w._recent_texts_at[gid] = []
vetoed = 0
for t, r in rows:
    if t < start:
        continue
    txt = r["content"].replace("\n", " ").strip()
    if r["role"] != "assistant":
        continue
    name = r["sender_name"]
    own = [s for (st, sn, s) in reversed(sent) if sn == name][:6]
    # 時間軸錨在「候選當下」：整份語料的年齡用現在回推，重播過多久都不會失真
    # （直接用資料的絕對時間戳，重播晚一小時 14:06 就會變成三小時前而被放行）。
    now = time.time()
    w._recent_texts_at[gid] = [now - (t - st).total_seconds() for (st, _sn, _s) in sent]
    v = w._local_veto(txt, own, "", gid)
    if v:
        vetoed += 1
    print(("VETO " if v else "pass "), t.strftime("%H:%M:%S"), name, "|", txt[:50], "|", v)
    sent.append((t, name, txt))
    w._recent_texts_by_group[gid].append(txt)
print(f"\n{vetoed} vetoed / {len(sent)}")
