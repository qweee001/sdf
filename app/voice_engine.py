"""台灣腔語音引擎：聲音資產庫（Voice Profile）→ 任意文字 → Telegram 原生語音條。

設計原則（與舊的「固定語音片段庫」根本不同）：
- **資產是「聲線」不是「句子」**：一個 VoiceProfile 定義 voice/rate/pitch/
  後處理鏈，可以合成**任何**文字，不受預錄台詞限制。
- **輸出原生語音條**：OGG/Opus 48kHz 單聲道 + DocumentAttributeAudio(voice=True)，
  Telegram 顯示綠色圓形播放器與波形條，不是帶檔名的音訊附件。
- **真人感後處理**：輕微環境底噪（room tone）＋可選音量正規化，去掉 AI
  TTS 的「無菌實驗室」死寂感。
- **可插拔後端**：edge-tts 為預設（免費、台灣神經女聲），
  之後要換 GPT-SoVITS / ElevenLabs / IndexTTS2 只改 provider。

用法：
    from app.voice_engine import VoiceEngine, VoiceProfile

    engine = VoiceEngine()
    ogg = await engine.synthesize(profile, "笑死，你們真的很會懷疑欸")
"""
from __future__ import annotations

import asyncio
import json
import random
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# 預設四角色的「定版聲線」（可在 dashboard 調整，或改 voices.json 覆寫）
DEFAULT_PROFILES: dict[str, dict] = {
    "小小": {
        "provider": "edge",
        "voice": "zh-TW-HsiaoChenNeural",
        "rate": "+4%",
        "pitch": "+2Hz",
        "style": "28 歲台北金融輕熟女，下班微喘、自然日常",
        "room_tone": 0.15,
    },
    "小天後": {
        "provider": "edge",
        "voice": "zh-TW-HsiaoYuNeural",
        "rate": "+16%",
        "pitch": "+0Hz",
        "style": "30 歲新北櫃姐，語速快、扁平嗆辣",
        "room_tone": 0.15,
    },
    "RICH": {
        "provider": "edge",
        "voice": "zh-TW-HsiaoChenNeural",
        "rate": "-8%",
        "pitch": "-6Hz",
        "style": "28 歲桃園設計師，慵懶內斂、微醺氣質",
        "room_tone": 0.15,
    },
    "Chen": {
        "provider": "edge",
        "voice": "zh-TW-HsiaoYuNeural",
        "rate": "-12%",
        "pitch": "-12Hz",
        "style": "38 歲台北大安服飾店老闆娘，成熟磁性、寵溺調侃",
        "room_tone": 0.15,
    },
}


@dataclass
class VoiceProfile:
    """一個「聲線資產」：可以合成任意文字的完整配方。"""

    name: str
    provider: str = "edge"
    voice: str = "zh-TW-HsiaoChenNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    style: str = ""
    # 真人感後處理
    room_tone: float = 0.15      # 環境底噪混合比例（0~1）
    normalize: bool = True       # 音量正規化
    extra_filters: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, raw: dict) -> "VoiceProfile":
        return cls(
            name=name,
            provider=str(raw.get("provider") or "edge"),
            voice=str(raw.get("voice") or "zh-TW-HsiaoChenNeural"),
            rate=str(raw.get("rate") or "+0%"),
            pitch=str(raw.get("pitch") or "+0Hz"),
            volume=str(raw.get("volume") or "+0%"),
            style=str(raw.get("style") or ""),
            room_tone=float(raw.get("room_tone", 0.15) or 0.0),
            normalize=bool(raw.get("normalize", True)),
            extra_filters=[str(f) for f in (raw.get("extra_filters") or [])],
        )

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "voice": self.voice,
            "rate": self.rate,
            "pitch": self.pitch,
            "volume": self.volume,
            "style": self.style,
            "room_tone": self.room_tone,
            "normalize": self.normalize,
            "extra_filters": list(self.extra_filters),
        }


class VoiceEngineError(Exception):
    """語音合成失敗（後端不可用、文字為空、轉檔失敗）。"""


class VoiceEngine:
    """聲音資產庫：VoiceProfile → 任意文字 → OGG/Opus 語音條位元組。

    不依賴任何預錄素材；每個 profile 都是「能生成任何語音」的資產。
    """

    # Telegram 語音條規格
    OPUS_RATE = 48000
    OPUS_BITRATE = "32k"

    def __init__(self, profiles: dict[str, dict] | None = None):
        merged = dict(DEFAULT_PROFILES)
        if profiles:
            merged.update(profiles)
        self.profiles: dict[str, VoiceProfile] = {
            name: VoiceProfile.from_dict(name, raw)
            for name, raw in merged.items()
        }

    # ---- 資產庫管理 ----
    def get(self, name: str) -> VoiceProfile | None:
        return self.profiles.get(str(name))

    def names(self) -> list[str]:
        return sorted(self.profiles)

    def upsert(self, profile: VoiceProfile) -> None:
        self.profiles[profile.name] = profile

    def to_dict(self) -> dict:
        return {n: p.to_dict() for n, p in self.profiles.items()}

    @classmethod
    def from_json_file(cls, path: str | Path) -> "VoiceEngine":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        return cls(raw if isinstance(raw, dict) else {})

    # ---- 合成 ----
    async def synthesize(self, profile: VoiceProfile, text: str) -> bytes:
        """把任意文字合成成 Telegram 原生語音條（OGG/Opus 位元組）。"""
        body = str(text or "").strip()
        if not body:
            raise VoiceEngineError("合成文字為空")
        if profile.provider != "edge":
            raise VoiceEngineError(f"不支援的語音後端：{profile.provider}")

        tmpdir = Path(tempfile.mkdtemp(prefix="sdf_voice_"))
        try:
            mp3 = tmpdir / "raw.mp3"
            await self._edge_synth(profile, body, mp3)
            ogg = tmpdir / "out.ogg"
            self._to_voice_note(profile, mp3, ogg)
            data = ogg.read_bytes()
            if not data:
                raise VoiceEngineError("轉檔後語音為空")
            return data
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def _edge_synth(
        self, profile: VoiceProfile, text: str, dest: Path
    ) -> None:
        try:
            import edge_tts  # 延遲匯入：沒裝套件時其他功能不受影響
        except ImportError as exc:
            raise VoiceEngineError("edge-tts 未安裝") from exc
        try:
            comm = edge_tts.Communicate(
                text,
                profile.voice,
                rate=profile.rate,
                pitch=profile.pitch,
                volume=profile.volume,
            )
            await comm.save(str(dest))
        except Exception as exc:
            raise VoiceEngineError(f"TTS 合成失敗：{exc}") from exc
        if not dest.exists() or dest.stat().st_size == 0:
            raise VoiceEngineError("TTS 沒有產生音訊")

    def _to_voice_note(
        self, profile: VoiceProfile, src: Path, dest: Path
    ) -> None:
        """轉成 Telegram 原生語音條：Opus 48k 單聲道 + 可選真人感後處理。"""
        filters: list[str] = []
        if profile.normalize:
            filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
        filters.extend(profile.extra_filters)
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
        ]
        if filters:
            cmd += ["-af", ",".join(filters)]
        cmd += [
            "-c:a", "libopus",
            "-b:a", self.OPUS_BITRATE,
            "-ar", str(self.OPUS_RATE),
            "-ac", "1",
            "-f", "ogg", str(dest),
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", "replace")[:300]
            raise VoiceEngineError(f"ffmpeg 轉檔失敗：{err}")
        if not dest.exists() or dest.stat().st_size == 0:
            raise VoiceEngineError("ffmpeg 沒有產生語音檔")

    # ---- 長度估算（Telegram 波形用）----
    @staticmethod
    def ogg_duration_seconds(data: bytes) -> int:
        """解析 Ogg granule position 估算秒數（Opus 固定 48kHz）。"""
        try:
            if b"OpusHead" not in data:
                return 5
            pos = 0
            last = 0
            while True:
                start = data.find(b"OggS", pos)
                if start < 0 or start + 27 > len(data):
                    break
                granule = int.from_bytes(data[start + 6:start + 14], "little")
                if granule > 0:
                    last = granule
                pos = start + 27 + data[start + 26]
            if last <= 0:
                return 5
            return max(1, int(last / 48000))
        except Exception:
            return 5
