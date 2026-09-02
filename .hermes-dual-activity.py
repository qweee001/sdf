import json
import sqlite3
import time

conn = sqlite3.connect("file:/data/chat.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
now = time.time()
groups = [-1002229799107, -1002197448156]
managed = {
    int(row[0])
    for row in conn.execute(
        "SELECT tg_user_id FROM accounts WHERE tg_user_id IS NOT NULL"
    )
}


def normalize(text):
    return " ".join(str(text or "").casefold().split())


def is_managed(row):
    return int(row["sender_id"]) in managed or str(row["role"]).lower() == "assistant"


output = {}
for group_id in groups:
    rows = conn.execute(
        "SELECT account_id, sender_id, role, content, timestamp "
        "FROM messages WHERE group_id=? AND timestamp>=? ORDER BY timestamp",
        (group_id, now - 3600),
    ).fetchall()
    seen = set()
    events = []
    for row in rows:
        key = (
            int(row["sender_id"]),
            normalize(row["content"]),
            int(float(row["timestamp"]) // 5),
        )
        if key in seen:
            continue
        seen.add(key)
        events.append(row)
    output[str(group_id)] = {
        "raw_60m": len(rows),
        "dedup_60m": len(events),
        "managed_60m": sum(is_managed(row) for row in events),
        "human_60m": sum(not is_managed(row) for row in events),
        "managed_15m": sum(
            is_managed(row) and float(row["timestamp"]) >= now - 900
            for row in events
        ),
        "human_15m": sum(
            not is_managed(row) and float(row["timestamp"]) >= now - 900
            for row in events
        ),
        "distinct_managed_senders_60m": len(
            {int(row["sender_id"]) for row in events if is_managed(row)}
        ),
        "last_age_min": (
            round((now - max(float(row["timestamp"]) for row in events)) / 60, 1)
            if events
            else None
        ),
    }
print(json.dumps({"now": now, "groups": output}, separators=(",", ":")))
