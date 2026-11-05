"""對真實水軍發言回放 _local_veto，量測誤殺率。

時間軸錨在「候選那一則當下」：舊句年齡用真實間隔算，不因為跑得晚而自動過期
（652fac3 修過一次，這裡把自己的場景桶一起補上）。

用法：REC=<記錄 json> START=17:09 END=18:10 python -m tools.replay_veto
"""
import json
from types import SimpleNamespace
from typing import Any, cast
import datetime, os, zoneinfo

from app.worker import AccountWorker

cfg = SimpleNamespace(ai_model="m", ai_temperature=0.8, ai_max_tokens=200, ai_timeout=17,
                      ai_disable_thinking=True, memory_max_messages=10, min_typing_delay=0,
                      max_typing_delay=0, media_enabled=True, media_max_input_bytes=1, decision_enabled=False)
GID = -1001
SHARED: dict = {}
OWN: dict = {}


def worker_for(name: str) -> AccountWorker:
    """一個帳號一個 worker：跨號桶共用，自己的場景桶各自一份（正式環境就是這樣）。"""
    if name in SHARED:
        OWN[name].setdefault(GID, [])
        SHARED[name]._own_scene_by_group = {GID: OWN[name][GID]}
        return SHARED[name]
    w = AccountWorker(account_id=name, session_key="k", tg_api_id=1, tg_api_hash="h",
                      ai_client=cast(Any, None), db=cast(Any, SimpleNamespace()), config=cfg,
                      managed_ids=set(), on_status_change=lambda *a, **k: None, selected_groups=[GID])
    w.persona = {"name": name, "gender": "女", "age": 30}
    SHARED[name] = w
    OWN[name] = {GID: []}
    w._own_scene_by_group = OWN[name]
    return w

PATH = os.environ.get("REC", r"D:/Hello/AppData/Local/hermes/cache/scratch/rec500.json")
START = os.environ.get("START", "17:09")
END = os.environ.get("END", "23:59")
d = json.load(open(PATH, encoding="utf-8"))
tz = zoneinfo.ZoneInfo("Asia/Taipei")
rows = sorted([(datetime.datetime.fromtimestamp(r["timestamp"], tz), r) for r in d["records"]], key=lambda x: x[0])
_h, _m = (int(x) for x in START.split(":"))
_e, _f = (int(x) for x in END.split(":"))
day = rows[-1][0].date()
start = datetime.datetime.combine(day, datetime.time(_h, _m), tzinfo=tz)
end = datetime.datetime.combine(day, datetime.time(_e, _f), tzinfo=tz)
gid = GID
sent = []          # (time, name, text)
TEXTS: list[str] = []
STAMPS: list[float] = []
vetoed = 0
real_now = datetime.datetime.now(tz)

for t, r in rows:
    if t < start:
        continue
    if t > end:
        break
    txt = r["content"].replace("\n", " ").strip()
    if r["role"] != "assistant":
        continue
    name = r["sender_name"]
    w = worker_for(name)
    # 時鐘撥回候選當下，讓 age 依真實間隔計算
    shift = (real_now - t).total_seconds()
    w._recent_texts_by_group[gid] = TEXTS
    w._recent_texts_at[gid] = [ts + shift for ts in STAMPS]
    w._own_scene_by_group[gid] = [
        (b, ts + shift) for b, ts in w._own_scene_by_group[gid]
    ]
    own = [s for (st, sn, s) in reversed(sent) if sn == name][:6]
    v = w._local_veto(txt, own, "", gid)
    if v:
        vetoed += 1
    print(("VETO " if v else "pass "), t.strftime("%H:%M:%S"), name, "|", txt[:46], "|", v)
    sent.append((t, name, txt))
    TEXTS.append(txt)
    STAMPS.append(real_now.timestamp())
    w._own_scene_by_group[gid].append((txt, real_now.timestamp()))
    del TEXTS[:-50]
    del STAMPS[:-50]
print(f"\n{vetoed} vetoed / {len(sent)}")