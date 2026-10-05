"""臨時：量測生產 AI 端點的回應時間（懷疑 35B + thinking 造成 5 分鐘級節奏）。"""
import os
import time
from openai import OpenAI

key = os.environ["AI_API_KEY"]
client = OpenAI(api_key=key, base_url=os.environ["AI_BASE_URL"])

messages = [
    {"role": "system", "content": "你是台灣女生，在約會群裡聊天。一句短訊、繁體中文、不要句號。"},
    {"role": "user", "content": "[阿宏] 今天加班到十點\n[美玲] 你也太拼了\n現在台北時間 22 點（深夜）。接一句話。"},
]

for model in ("qwen3.6-35b-a3b",):
    for thinking in (False, True):
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=512,
                temperature=0.85,
                extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
            )
            dt = time.time() - t0
            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            print(f"{model} thinking={thinking} {dt:.1f}s out={usage.completion_tokens if usage else '?'} tok :: {text[:60]!r}")
        except Exception as exc:
            print(f"{model} thinking={thinking} FAILED after {time.time()-t0:.1f}s: {exc}")
