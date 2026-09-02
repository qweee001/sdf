import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict

DB = "file:/data/chat.db?mode=ro"
conn = sqlite3.connect(DB, uri=True)
conn.row_factory = sqlite3.Row
now = time.time()
accounts = conn.execute(
    "SELECT id, tg_user_id, persona, groups, setup_complete, enabled FROM accounts"
).fetchall()
managed_ids = {int(r["tg_user_id"]) for r in accounts if r["tg_user_id"]}
selected = {}
for row in accounts:
    try:
        groups = [int(v) for v in json.loads(row["groups"] or "[]")]
    except Exception:
        groups = []
    selected[row["id"]] = groups

rows = conn.execute(
    "SELECT group_id, sender_id, role, content, timestamp "
    "FROM messages WHERE timestamp >= ? ORDER BY timestamp",
    (now - 7 * 86400,),
).fetchall()

def norm(text):
    return " ".join(str(text or "").casefold().split())

seen = set()
dedup = []
for row in rows:
    key = (
        int(row["group_id"]),
        int(row["sender_id"]),
        norm(row["content"]),
        int(float(row["timestamp"]) // 5),
    )
    if key in seen:
        continue
    seen.add(key)
    dedup.append(row)

hour_human = Counter()
day_kind = Counter()
group_stats = defaultdict(lambda: Counter())
last_human = {}
last_managed = {}
for row in dedup:
    gid = int(row["group_id"])
    sid = int(row["sender_id"])
    ts = float(row["timestamp"])
    managed = sid in managed_ids or str(row["role"]).lower() == "assistant"
    kind = "managed" if managed else "human"
    hkt = time.gmtime(ts + 8 * 3600)
    day = time.strftime("%Y-%m-%d", hkt)
    hour = hkt.tm_hour
    day_kind[(day, kind)] += 1
    group_stats[gid][kind] += 1
    if managed:
        last_managed[gid] = max(last_managed.get(gid, 0), ts)
    else:
        hour_human[hour] += 1
        last_human[gid] = max(last_human.get(gid, 0), ts)

out = {
    "window_days": 7,
    "raw_rows": len(rows),
    "deduplicated_events": len(dedup),
    "dedupe_key": "group+sender+normalized_content+5s_bucket",
    "accounts": len(accounts),
    "enabled_accounts": sum(int(bool(r["enabled"])) for r in accounts),
    "selected_groups_per_account": {k: len(v) for k, v in selected.items()},
    "unique_selected_groups": len({g for values in selected.values() for g in values}),
    "selected_group_tags": sorted({
        hashlib.sha256(str(g).encode()).hexdigest()[:8]
        for values in selected.values() for g in values
    }),
    "busiest_human_hours_hkt": [
        {"hour": h, "events": c} for h, c in hour_human.most_common(8)
    ],
    "daily": [
        {"day": d, "human": day_kind[(d, "human")], "managed": day_kind[(d, "managed")]}
        for d in sorted({k[0] for k in day_kind})
    ],
    "groups": [],
}
for gid, counts in group_stats.items():
    tag = hashlib.sha256(str(gid).encode()).hexdigest()[:8]
    out["groups"].append({
        "group_tag": tag,
        "human": counts["human"],
        "managed": counts["managed"],
        "last_human_age_minutes": round((now - last_human.get(gid, 0)) / 60, 1) if last_human.get(gid) else None,
        "last_managed_age_minutes": round((now - last_managed.get(gid, 0)) / 60, 1) if last_managed.get(gid) else None,
    })
out["groups"].sort(key=lambda x: x["group_tag"])
print(json.dumps(out, ensure_ascii=False, indent=2))
