import base64
import hashlib
import json
import re
import sqlite3
import statistics
import time
import zlib

DB = "/data/chat.db"
TARGETS = [-1002229799107, -1002197448156]
LABELS = {TARGETS[0]: "G1", TARGETS[1]: "G2"}


def norm(text):
    return " ".join(str(text or "").split()).strip()


def redact(text):
    text = norm(text)
    text = re.sub(r"https?://\S+", "[URL]", text, flags=re.I)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[EMAIL]", text)
    text = re.sub(r"(?<!\w)@[A-Za-z0-9_]{3,}", "[HANDLE]", text)
    text = re.sub(r"(?<!\d)(?:\+?886[- ]?)?0?9\d{2}[- ]?\d{3}[- ]?\d{3}(?!\d)", "[PHONE]", text)
    text = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[NUMBER]", text)
    return text


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * p
    lo = int(pos)
    hi = min(len(values) - 1, lo + 1)
    return round(values[lo] + (values[hi] - values[lo]) * (pos - lo), 3)


conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
accounts = conn.execute("SELECT id,tg_user_id,groups FROM accounts ORDER BY created_at,id").fetchall()
managed_ids = {int(row["tg_user_id"]): f"M{i+1}" for i, row in enumerate(accounts) if row["tg_user_id"] is not None}

report = {
    "generated_at": time.time(),
    "dedupe": "human=(group,sender,normalized content,5-second bucket); managed=assistant rows",
    "retention_note": "messages table retains at most 200 rows per account per group",
    "groups": {},
}

for gid in TARGETS:
    rows = conn.execute(
        "SELECT id,account_id,sender_id,role,content,timestamp FROM messages WHERE group_id=? ORDER BY timestamp,id",
        (gid,),
    ).fetchall()
    managed = []
    human_candidates = []
    for row in rows:
        sender_id = int(row["sender_id"] or 0)
        text = redact(row["content"])
        if row["role"] == "assistant" and sender_id in managed_ids:
            managed.append({"at": float(row["timestamp"]), "sender": managed_ids[sender_id], "kind": "managed", "text": text})
        elif row["role"] == "user" and sender_id not in managed_ids:
            human_candidates.append((sender_id, float(row["timestamp"]), text))

    human_seen = set()
    humans = []
    for sender_id, ts, text in human_candidates:
        key = (sender_id, norm(text), int(ts // 5))
        if key in human_seen:
            continue
        human_seen.add(key)
        tag = "H" + hashlib.sha256(str(sender_id).encode()).hexdigest()[:6]
        humans.append({"at": ts, "sender": tag, "kind": "human", "text": text})

    timeline_events = sorted(managed + humans, key=lambda item: (item["at"], item["kind"] != "human"))
    events = [
        {
            "hkt": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(event["at"] + 8 * 3600)),
            "sender": event["sender"],
            "kind": event["kind"],
            "text": event["text"],
        }
        for event in timeline_events
    ]

    managed_texts = [event["text"] for event in managed]
    human_texts = [text for _, _, text in human_candidates]
    sender_counts = {}
    for event in managed:
        sender_counts[event["sender"]] = sender_counts.get(event["sender"], 0) + 1
    mtimes = sorted(event["at"] for event in managed)
    gaps = [b - a for a, b in zip(mtimes, mtimes[1:])]
    repeat_counts = {}
    for text in managed_texts:
        repeat_counts[text] = repeat_counts.get(text, 0) + 1
    top_repeats = [
        {"count": count, "text": text}
        for text, count in sorted(repeat_counts.items(), key=lambda item: (-item[1], item[0]))
        if text and count >= 2
    ][:20]
    meta_re = re.compile(r"群規|收費|費用|付款|安全保障|管理員|助理|審核|門檻|一千|1000|會員制")
    ai_re = re.compile(r"作為.{0,3}AI|身為.{0,3}AI|無法協助|不能協助|不便提供|我不能")
    mainland_re = re.compile(r"视频|信息|软件|账号|网红|挺好|啥|妹子|认识新朋友|觉得好孤单|在線等")
    meta_examples = [text for text in managed_texts if meta_re.search(text)][:20]
    ai_examples = [text for text in managed_texts if ai_re.search(text)][:20]
    mainland_examples = [text for text in managed_texts if mainland_re.search(text)][:20]

    consecutive = []
    run = 0
    longest = 0
    for event in events:
        if event["kind"] == "managed":
            run += 1
            longest = max(longest, run)
        else:
            if run:
                consecutive.append(run)
            run = 0
    if run:
        consecutive.append(run)

    # Approximate pile-on: managed messages within 60s after one deduped human event, before next human.
    pileons = []
    timeline = sorted(managed + humans, key=lambda item: item["at"])
    for idx, event in enumerate(timeline):
        if event["kind"] != "human":
            continue
        replies = []
        for later in timeline[idx + 1:]:
            if later["kind"] == "human":
                break
            if later["at"] - event["at"] > 60:
                break
            replies.append(later)
        if len(replies) > 1:
            pileons.append({"human": event["text"], "managed_count": len(replies), "managed": [r["text"] for r in replies[:5]]})

    reply_rows = conn.execute(
        "SELECT message_id,stage,reason,account_id,at FROM reply_events WHERE group_id=? ORDER BY at",
        (gid,),
    ).fetchall()
    sent_per_message = {}
    problem_stages = {}
    for row in reply_rows:
        stage = str(row["stage"] or "")
        if stage == "sent":
            key = int(row["message_id"] or 0)
            sent_per_message.setdefault(key, set()).add(str(row["account_id"] or ""))
        elif stage != "claimed":
            key = stage + ":" + str(row["reason"] or "")
            problem_stages[key] = problem_stages.get(key, 0) + 1

    report["groups"][LABELS[gid]] = {
        "raw_rows": len(rows),
        "managed_sent": len(managed),
        "human_candidates": len(human_candidates),
        "human_deduped": len(humans),
        "managed_sender_counts": sender_counts,
        "managed_text_length": {
            "median": percentile([len(text) for text in managed_texts], 0.5),
            "p90": percentile([len(text) for text in managed_texts], 0.9),
        },
        "managed_gap_seconds": {
            "median": percentile(gaps, 0.5),
            "p95": percentile(gaps, 0.95),
            "max": round(max(gaps), 3) if gaps else None,
        },
        "exact_duplicate_count": len(managed_texts) - len(set(managed_texts)),
        "top_repeats": top_repeats,
        "meta_count": len(meta_examples),
        "meta_examples": meta_examples,
        "ai_refusal_count": len(ai_examples),
        "ai_refusal_examples": ai_examples,
        "mainland_term_count": len(mainland_examples),
        "mainland_term_examples": mainland_examples,
        "longest_managed_chain": longest,
        "pileon_candidates": pileons[:20],
        "reply_events_sent_multi_account": sum(1 for accounts_set in sent_per_message.values() if len(accounts_set) > 1),
        "reply_problem_stages": problem_stages,
        "events": events,
    }

pending = conn.execute("SELECT COUNT(*) FROM outbound_claims WHERE claim_key LIKE 'continuous-pending:%'").fetchone()[0]
report["continuous_pending"] = int(pending or 0)
payload = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
encoded = base64.b64encode(zlib.compress(payload, 9)).decode("ascii")
print("SDF_ALL_DIALOGUE_ZLIB=" + encoded)
conn.close()
