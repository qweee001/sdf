"""生成台灣腔語音模板候選（給主人試聽定版）。

用法：
    python scripts/voice_template_candidates.py

輸出：voice_candidates/*.ogg（Telegram 原生語音條格式）與 *.mp3（試聽）
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import edge_tts

OUT = Path(__file__).resolve().parent.parent / "voice_candidates"
OUT.mkdir(exist_ok=True)

# 每個角色 3 組候選模板：voice + rate + pitch（+ 選配 ffmpeg 後處理）
# 台詞故意用同一句，方便主人純聽「聲線差異」。
SAMPLE_TEXT = "笑死，你們真的很會懷疑欸，我下班累死了啦"

TEMPLATES: dict[str, list[dict]] = {
    "小小_28歲金融輕熟女": [
        {
            "id": "xiao_A",
            "voice": "zh-TW-HsiaoChenNeural",
            "rate": "+12%",
            "pitch": "+12Hz",
            "desc": "A 甜亮活潑（偏高偏快，下班微喘的輕熟撒嬌）",
        },
        {
            "id": "xiao_B",
            "voice": "zh-TW-HsiaoChenNeural",
            "rate": "+4%",
            "pitch": "+2Hz",
            "desc": "B 自然日常（原聲基準，最像真人講話）",
        },
        {
            "id": "xiao_C",
            "voice": "zh-TW-HsiaoYuNeural",
            "rate": "+10%",
            "pitch": "+6Hz",
            "desc": "C 軟糯娃娃音（另一個女聲，偏甜偏軟）",
        },
    ],
    "小天後_30歲櫃姐嗆辣": [
        {
            "id": "tian_A",
            "voice": "zh-TW-HsiaoYuNeural",
            "rate": "+16%",
            "pitch": "+0Hz",
            "desc": "A 嗆辣俐落（語速快、扁平、台北機車妹）",
        },
        {
            "id": "tian_B",
            "voice": "zh-TW-HsiaoChenNeural",
            "rate": "+18%",
            "pitch": "-4Hz",
            "desc": "B 直球務實（偏低偏快，大姐頭感）",
        },
        {
            "id": "tian_C",
            "voice": "zh-TW-HsiaoYuNeural",
            "rate": "+8%",
            "pitch": "+8Hz",
            "desc": "C 輕快俏皮（沒那麼兇，帶笑意）",
        },
    ],
    "RICH_28歲設計師慵懶": [
        {
            "id": "rich_A",
            "voice": "zh-TW-HsiaoChenNeural",
            "rate": "-8%",
            "pitch": "-6Hz",
            "desc": "A 慵懶低沉（慢、鬆、微醺感）",
        },
        {
            "id": "rich_B",
            "voice": "zh-TW-HsiaoYuNeural",
            "rate": "-4%",
            "pitch": "-2Hz",
            "desc": "B 內斂文靜（話少、氣質、語尾輕）",
        },
        {
            "id": "rich_C",
            "voice": "zh-TW-HsiaoChenNeural",
            "rate": "+0%",
            "pitch": "+4Hz",
            "desc": "C 輕柔反問（溫柔中帶勾）",
        },
    ],
    "Chen_38歲熟女磁性": [
        {
            "id": "chen_A",
            "voice": "zh-TW-HsiaoYuNeural",
            "rate": "-12%",
            "pitch": "-12Hz",
            "desc": "A 成熟磁性（低、穩、寵溺調侃）",
        },
        {
            "id": "chen_B",
            "voice": "zh-TW-HsiaoChenNeural",
            "rate": "-10%",
            "pitch": "-8Hz",
            "desc": "B 知性溫潤（從容、有包容感）",
        },
        {
            "id": "chen_C",
            "voice": "zh-TW-HsiaoYuNeural",
            "rate": "-6%",
            "pitch": "-16Hz",
            "desc": "C 低啞性感（偏沙啞、氣音感最重）",
        },
    ],
}


async def synth(voice: str, text: str, rate: str, pitch: str, dest: Path) -> None:
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await comm.save(str(dest))


def to_ogg(mp3: Path, ogg: Path) -> bool:
    """轉成 Telegram 原生語音條（OGG/Opus 48kHz 單聲道）。"""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(mp3),
        "-c:a", "libopus", "-b:a", "32k",
        "-ar", "48000", "-ac", "1",
        "-f", "ogg", str(ogg),
    ]
    return subprocess.run(cmd, capture_output=True).returncode == 0


async def main() -> None:
    for persona, items in TEMPLATES.items():
        for item in items:
            mp3 = OUT / f"{item['id']}.mp3"
            ogg = OUT / f"{item['id']}.ogg"
            try:
                await synth(
                    item["voice"], SAMPLE_TEXT,
                    item["rate"], item["pitch"], mp3,
                )
                ok = to_ogg(mp3, ogg)
                size = ogg.stat().st_size if ok else 0
                print(
                    f"[OK] {item['id']:8s} {persona:22s} "
                    f"{item['voice']:24s} rate={item['rate']:5s} "
                    f"pitch={item['pitch']:6s} ogg={size}B  {item['desc']}"
                )
            except Exception as exc:
                print(f"[FAIL] {item['id']} {exc}")


if __name__ == "__main__":
    asyncio.run(main())
