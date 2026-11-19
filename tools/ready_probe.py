"""外部就緒探針：打 /ready，非 200 就以非 0 結束（給 cron / 監控 / CI 用）。

為什麼需要一支獨立探針：Railway 的 healthcheckPath 只決定「這個容器算不算活」，
它不會告警、也不會重啟一個 200 但功能全啞的服務。/ready 的 503 需要有人去看，
這支就是那個「有人」。

用法：
    python tools/ready_probe.py https://sdf-production-4f52.up.railway.app
    python tools/ready_probe.py --json <url>     # 印出完整 payload 判斷原因
無參數時讀 SDF_BASE_URL。

退出碼：0 就緒、1 未就緒、2 連不上。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 15.0


def probe(base_url: str, show_json: bool) -> int:
    url = base_url.rstrip("/") + "/ready"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        code = exc.code
    except Exception as exc:
        print(f"UNREACHABLE {url}: {exc}", file=sys.stderr)
        return 2

    try:
        payload = json.loads(body)
    except Exception:
        print(f"HTTP {code} 非 JSON 回應：{body[:300]}", file=sys.stderr)
        return 1

    if show_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        accounts = payload.get("accounts") or {}
        metrics = payload.get("metrics") or {}
        print(
            "HTTP {code} status={status} running={running}/{total} muted={muted} "
            "silent={silent}min ai_calls_24h={ai} lag={lag}ms".format(
                code=code,
                status=payload.get("status"),
                running=accounts.get("running"),
                total=accounts.get("total"),
                muted=accounts.get("muted"),
                silent=accounts.get("silent_minutes"),
                ai=payload.get("ai_calls_24h"),
                lag=metrics.get("loop_lag_ms"),
            )
        )
    return 0 if code == 200 else 1


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--json"]
    show_json = "--json" in argv[1:]
    base_url = args[0] if args else os.environ.get("SDF_BASE_URL", "")
    if not base_url:
        print("用法：ready_probe.py [--json] <base_url>（或設 SDF_BASE_URL）", file=sys.stderr)
        return 2
    return probe(base_url, show_json)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
