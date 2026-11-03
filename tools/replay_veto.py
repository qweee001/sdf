"""對真實 19:08-20:10 水軍發言回放 _local_veto，量測誤殺率。"""
import json
from types import SimpleNamespace
from typing import Any, cast
import datetime, zoneinfo

from app.worker import AccountWorker

cfg = SimpleNamespace(ai_model="m", ai_temperature=0.8, ai_max_tokens=200, ai_timeout=17,
                      ai_disable_thinking=True, memory_max_messages=10, min_typing_delay=0,
                      max_typing_delay=0, media_enabled=True, media_max_input_bytes=1, decision_enabled=False)
w = AccountWorker(account_id="w", session_key="k", tg_api_id=1, tg_api_hash="h",
                  ai_client=cast(Any, None), db=cast(Any, SimpleNamespace()), config=cfg,
                  managed_ids=set(), on_status_change=lambda *a, **k: None, selected_groups=[-1001])
w.persona = {"name": "Chen", "gender": "女", "age": 30}

import os
PATH = os.environ.get("REC", r"D:/Hello/AppData/Local/hermes/cache/scratch/rec500.json")
START = os.environ.get("START", "19:08")
d = json.load(open(PATH, encoding="utf-8"))
tz = zoneinfo.ZoneInfo("Asia/Taipei")
rows = sorted([(datetime.datetime.fromtimestamp(r["timestamp"], tz), r) for r in d["records"]], key=lambda x: x[0])
_h, _m = (int(x) for x in START.split(":"))
start = datetime.datetime(2026, 11, 3, _h, _m, tzinfo=tz)
gid = -1001
sent = []          # (time, name, text)
w._recent_texts_by_group[gid] = []
vetoed = 0
for t, r in rows:
    if t < start:
        continue
    txt = r["content"].replace("\n", " ").strip()
    if r["role"] != "assistant":
        continue
    name = r["sender_name"]
    own = [s for (st, sn, s) in reversed(sent) if sn == name][:6]
    bucket = w._recent_texts_by_group[gid]
    cross = [s for s in bucket[-6:] if s not in set(own)]
    v = w._local_veto(txt, own, "", gid)
    if v:
        vetoed += 1
    print(("VETO " if v else "pass "), t.strftime("%H:%M:%S"), name, "|", txt[:46], "|", v)
    sent.append((t, name, txt))
    bucket.append(txt)
    del bucket[:-50]
print(f"\n{vetoed} vetoed / {len(sent)}")
