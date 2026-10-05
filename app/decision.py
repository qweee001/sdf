"""決策層（System One）：TypeSafe Jev / Laya 同協議的 /v1/systemone 端點。

三種問句（可一次混著問，並行回傳）：
- choice：給選項，回 choice + probabilities + confidence
- score：給量表，回 score + probabilities + confidence
- noul：是/否，回 0~1 的概率

換 Jev ↔ Laya 只改 base_url（laya-serve 講同一個 wire protocol），代碼零動。
失敗（超時／非 200／格式錯）一律擲 DecisionError，由呼叫方決定降級策略。
"""
from __future__ import annotations

import httpx


class DecisionError(Exception):
    """決策端點呼叫失敗（超時、非 200、answers 缺欄位）。"""


async def system_one(
    state: str,
    questions: dict,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict:
    """打一次 /v1/systemone，回 answers dict（key 對應 questions 的 key）。"""
    url = f"{base_url.rstrip('/')}/v1/systemone"
    payload = {"state": state, "model": model, "questions": questions}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise DecisionError(
                    f"http {resp.status_code}: {resp.text[:120]}"
                )
            data = resp.json()
    except DecisionError:
        raise
    except Exception as exc:
        raise DecisionError(str(exc)) from exc
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise DecisionError("missing answers")
    return answers
