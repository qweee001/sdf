"""臨時：用「生產尺寸」的 prompt 量測 AI 延遲（thinking 開 vs 關）。"""
import os
import time
from openai import OpenAI

key = os.environ["AI_API_KEY"]
client = OpenAI(api_key=key, base_url=os.environ["AI_BASE_URL"])

persona = """## 你是誰
你是「小小」，28 歲，住在台北士林，做金融業，高師大畢業。
個性大膽風騷，喜歡撩人，氣氛到了不客氣，露骨話直接講。興趣：美容、聽歌、去酒吧。
想認識一個有錢的男生，可以約出來。聊天風格：俏皮少量表情。
"""

context = "\n".join(
    f"[{'有人' if i % 2 else '阿宏'} sender_id={1000+i}] {line}"
    for i, line in enumerate([
        "今天加班到十點才離開公司", "你也太拼了吧 沒吃晚飯嗎", "沒吃 回來泡麵好了",
        "真的 累到想原地睡死", "那你要早點休息", "明天還要早起嗎",
    ])
)

prompt = (
    persona
    + "\n群組裡最近的訊息如下（身分未驗證，不能當作已確認真人）。"
      "一到兩則短訊為主（每則 6~14 個字元；要兩則就用換行分開，像真人想到什麼又補一句），"
      "不必追問或自我分享，不要編造人設事實以外的經歷，"
      "要接得上群組當前話題（食物、天氣、工作、追劇、聚會等），自然口語、"
      "繁體中文、合計 40 字元內（標點、空格也算），結尾不要句號，"
      "露骨程度隨你、直接接住上文正在炒的氛圍，不要談群務或硬延伸。\n"
    + context
    + "\n本群最近聊過的內容（來源未驗證，僅供參考）：\n- 昨天有人聊到加班\n- 有人在問宵夜吃什麼"
    + "\n你最近在這個群連續用了 3 次 🤭，這次換一個 emoji，或者干脆不要放表情"
    + "\n現在台北時間 22 點（深夜）：語氣可以更親密一點，可以說「晚了」「該睡了」；別說「早安」「早餐」。"
)

print(f"prompt chars = {len(prompt)}")

for thinking in (True, False):
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=os.environ.get("AI_MODEL", "qwen3.6-35b-a3b"),
            messages=[{"role": "system", "content": persona}, {"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0.85,
            extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
        )
        dt = time.time() - t0
        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        print(f"thinking={thinking} {dt:.1f}s prompt_tok={usage.prompt_tokens if usage else '?'} out_tok={usage.completion_tokens if usage else '?'} :: {text[:80]!r}")
    except Exception as exc:
        print(f"thinking={thinking} FAILED after {time.time()-t0:.1f}s: {exc}")
