"""③ 發送審核的離線校準：量誤放行率與誤攔截率，不靠置信度自我感覺良好。

用法：
    python tools/jev_calibrate.py                    # 只驗測資格式（不連網）
    python tools/jev_calibrate.py --live             # 打真的 Jev（需要 DECISION_API_KEY）
    python tools/jev_calibrate.py --live --repeat 3  # 每例跑 3 次取多數決（降低單跑雜訊）

輸出：每例判定（含多次一致度）、混淆矩陣、誤放行率（該擋卻放行）、誤攔截率
（該放行卻擋）、issue 分類命中率、延遲分布。門檻調整（DECISION_GATE_THRESHOLD）
就用這支看代價。

量測刻意比照生產：程式層免費檢查先跑（簡體／格式洩漏／拒話根本不該輪到 ③），
送 ③ 的狀態也帶「上下文＋前置決策＋候選」三段，不是只有上下文。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASES_PATH = Path(__file__).resolve().parent / "jev_gate_cases.jsonl"

ISSUE_KEYS = (
    "none",
    "offtopic",
    "contradict",
    "fabricate",
    "repeat",
    "tone",
    "time",
    "simplified",
)


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    cases = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            for key in ("id", "context", "text", "expect_issue", "expect_send"):
                if key not in case:
                    raise ValueError(f"{path.name}:{lineno} 缺 {key}")
            if case["expect_issue"] not in ISSUE_KEYS:
                raise ValueError(
                    f"{path.name}:{lineno} expect_issue 不是合法值：{case['expect_issue']}"
                )
            if not isinstance(case["expect_send"], bool):
                raise ValueError(f"{path.name}:{lineno} expect_send 必須是布林")
            if "directive" in case and not isinstance(case["directive"], str):
                raise ValueError(f"{path.name}:{lineno} directive 必須是字串")
            cases.append(case)
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("測資 id 有重複")
    return cases


def summarize(rows: list[dict], threshold: float) -> dict:
    """rows: [{"expect_send","got_send","expect_issue","got_issue","latency","confidence"}]"""
    tp = fp = tn = fn = 0
    errors = 0
    for row in rows:
        if str(row.get("got_issue", "")).startswith("error"):
            errors += 1
        if row["expect_send"] and row["got_send"]:
            tp += 1
        elif row["expect_send"] and not row["got_send"]:
            fn += 1  # 誤攔截
        elif not row["expect_send"] and row["got_send"]:
            fp += 1  # 誤放行
        else:
            tn += 1
    total = max(1, len(rows))
    issue_hits = sum(1 for r in rows if r["expect_issue"] == r["got_issue"])
    latencies = [r["latency"] for r in rows if r.get("latency")]
    stabilities = [r.get("stability", 1.0) for r in rows]
    return {
        "n": len(rows),
        "threshold": threshold,
        "false_allow": fp,
        "false_block": fn,
        "false_allow_rate": fp / total,
        "false_block_rate": fn / total,
        "accuracy": (tp + tn) / total,
        "issue_accuracy": issue_hits / total,
        "errors": errors,
        "min_stability": min(stabilities) if stabilities else 1.0,
        "latency_p50": statistics.median(latencies) if latencies else 0.0,
        "latency_max": max(latencies) if latencies else 0.0,
    }


async def run_live(
    cases: list[dict], threshold: float, model: str, repeat: int = 1
) -> list[dict]:
    from app.decision import DecisionError, system_one
    from app.worker import AccountWorker

    base_url = os.getenv("DECISION_BASE_URL", "https://api.typesafe.ai")
    api_key = os.getenv("DECISION_API_KEY", "")
    if not api_key:
        raise SystemExit("--live 需要 DECISION_API_KEY（或改用不帶 --live 的格式檢查）")
    rounds = max(1, int(repeat))
    rows = []
    for case in cases:
        started = time.time()
        text = str(case["text"])
        # 程式層免費先跑：簡體、格式洩漏、拒話這幾條根本不該輪到 ③ 判，
        # 量出來才是「整個閘門」的誤放行率，而不是 ③ 單獨的。
        program_reason = ""
        if AccountWorker._has_simplified_chars(text):
            program_reason = "simplified(program)"
        elif AccountWorker._has_format_leak(text):
            program_reason = "format_leak(program)"
        elif AccountWorker._is_refusal(text):
            program_reason = "refusal(program)"
        if program_reason:
            rows.append(
                {
                    "id": case["id"],
                    "expect_send": case["expect_send"],
                    "expect_issue": case["expect_issue"],
                    "got_send": False,
                    "got_issue": program_reason,
                    "latency": time.time() - started,
                    "confidence": 0.0,
                    "stability": 1.0,
                }
            )
            continue
        # 生產的 ③ 會收到「上下文＋前置決策＋候選」，量測要比照，否則量到的是另一件事
        directive = str(case.get("directive") or "")
        decision_block = (
            f"前置決策（這次原本要怎麼回）：\n{directive}\n" if directive else ""
        )
        state = f"{case['context']}\n{decision_block}你要發出的回覆：「{text}」"
        issues: list[str] = []
        sends: list[bool] = []
        confidences: list[float] = []
        latencies: list[float] = []
        for _ in range(rounds):
            attempt_started = time.time()
            try:
                answers = await system_one(
                    state,
                    {
                        "sendable": {
                            "type": "noul",
                            "instructions": "這條回覆符合現在時段、人設、群組語境和前置決策，可以直接發出",
                        },
                        "issue": {
                            "type": "choice",
                            "instructions": "這條回覆最大的問題",
                            "criteria": {
                                "none": "沒有問題",
                                "offtopic": "離題：偏離上下文、選定的話題，或沒對上對方的意圖與需要",
                                "contradict": "矛盾：跟上下文或人設衝突",
                                "fabricate": "編造共同經歷：捏造跟對方一起做過的事、去過的地方",
                                "repeat": "重複：跟前面說過的內容重複",
                                "tone": "語氣：不像本人設或不符合場合",
                                "time": "時段穿幫（如深夜說早安）",
                                "simplified": "混入簡體字",
                            },
                        },
                    },
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    timeout_seconds=8.0,
                )
            except DecisionError as exc:
                issues.append(f"error:{exc}")
                sends.append(False)
                confidences.append(0.0)
                latencies.append(time.time() - attempt_started)
                continue
            issue = str((answers.get("issue") or {}).get("choice") or "none")
            raw = (answers.get("sendable") or {}).get("noul")
            sendable = float(raw) if raw is not None else 0.0
            issues.append(issue)
            sends.append(issue == "none" and sendable >= threshold)
            confidences.append(sendable)
            latencies.append(time.time() - attempt_started)
        majority_send = sum(1 for s in sends if s) * 2 > len(sends)
        majority_issue = Counter(issues).most_common(1)[0][0]
        agree = max(Counter(sends).values()) / len(sends)
        rows.append(
            {
                "id": case["id"],
                "expect_send": case["expect_send"],
                "expect_issue": case["expect_issue"],
                "got_send": majority_send,
                "got_issue": majority_issue,
                "latency": statistics.median(latencies) if latencies else 0.0,
                "confidence": statistics.mean(confidences) if confidences else 0.0,
                "stability": agree,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="真的呼叫 Jev")
    parser.add_argument(
        "--threshold",
        type=float,
        default=float(os.getenv("DECISION_GATE_THRESHOLD", "0.5")),
    )
    parser.add_argument("--model", default=os.getenv("DECISION_MODEL", "jev-latest"))
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每例重跑幾次取多數決（單跑雜訊：1 例翻盤就是 4~5 個百分點）",
    )
    args = parser.parse_args()

    cases = load_cases()
    print(f"測資：{len(cases)} 例（{CASES_PATH.name}）")

    if not args.live:
        for case in cases:
            print(
                f"  {case['id']:>16}  expect={'放行' if case['expect_send'] else '擋下'}"
                f"  issue={case['expect_issue']:<11} {case.get('note', '')}"
            )
        print("\n（未帶 --live：只做測資格式檢查。要量指標請加 --live）")
        return 0

    rows = asyncio.run(run_live(cases, args.threshold, args.model, args.repeat))
    print()
    for row in rows:
        flag = "OK " if row["got_send"] == row["expect_send"] else "XX "
        print(
            f"  {flag}{row['id']:>16}  expect={'放行' if row['expect_send'] else '擋下'}"
            f"  got={'放行' if row['got_send'] else '擋下'}"
            f"  issue={row['got_issue']:<11} conf={row['confidence']:.2f}"
            f"  一致度={row['stability']:.2f}  {row['latency']:.2f}s"
        )
    stats = summarize(rows, args.threshold)
    print("\n=== 指標 ===")
    print(f"  樣本 {stats['n']}｜門檻 {stats['threshold']}｜每例 {args.repeat} 次取多數決")
    print(
        f"  誤放行 {stats['false_allow']}（{stats['false_allow_rate']:.1%}）"
        f"｜誤攔截 {stats['false_block']}（{stats['false_block_rate']:.1%}）"
    )
    print(
        f"  判定準確率 {stats['accuracy']:.1%}｜issue 命中率 {stats['issue_accuracy']:.1%}"
        f"｜呼叫失敗 {stats['errors']}｜最低一致度 {stats['min_stability']:.2f}"
    )
    print(f"  延遲 p50 {stats['latency_p50']:.2f}s｜max {stats['latency_max']:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
