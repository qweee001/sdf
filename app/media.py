from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True)
class MediaAsset:
    kind: str
    data: bytes
    filename: str
    mime_type: str


class OrcaMediaService:
    """OrcaRouter 多模態與媒體生成；付費生成先走共用預算闸門。"""

    _MODEL_BUDGETS = {
        "vision": ("vision_model", "obsidian/Qwen3.8-27B", 0.10),
        "image": (
            "image_model",
            "google/imagen-4.0-fast-generate-001",
            0.03,
        ),
        "voice": ("speech_model", "openai/tts-1", 0.10),
        "video": ("video_model", "minimax/minimax-h3", 0.40),
    }
    _MAX_OUTPUT_BYTES = 50 * 1024 * 1024
    _SUGGESTIVE_IMAGE_POLICY = (
        "All people are fictional adult age 21+; sensual dating-app style is allowed, "
        "but no visible genitals, no explicit sex act, no real-person likeness, "
        "no minors. Natural candid smartphone photo. "
    )
    _SUGGESTIVE_VIDEO_POLICY = (
        "All people are fictional adults age 21+; flirtatious and sensual is allowed, "
        "but no nudity, no visible genitals, no explicit sex act, no real-person "
        "likeness, no minors. Natural vertical smartphone clip. "
    )

    def __init__(
        self,
        *,
        client: Any,
        db: Any,
        config: Any,
        http_client: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.client = client
        self.db = db
        self.config = config
        self._owns_http = http_client is None
        self.http = http_client or httpx.AsyncClient(
            timeout=float(config.media_generation_timeout)
        )
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def _reserve(self, account_id: str, kind: str) -> bool:
        field, expected_model, reserve_usd = self._MODEL_BUDGETS[kind]
        configured_model = str(getattr(self.config, field, ""))
        if configured_model != expected_model:
            raise ValueError(
                f"未核准的 {kind} 模型：{configured_model or '<empty>'}"
            )
        return await self.db.reserve_media_budget(
            account_id,
            kind,
            reserve_usd,
            float(self.config.media_daily_budget_usd),
        )

    async def understand_image(
        self,
        account_id: str,
        image: bytes,
        mime_type: str,
        system_prompt: str,
        user_text: str,
    ) -> str:
        if not await self._reserve(account_id, "vision"):
            return ""
        encoded = base64.b64encode(image).decode("ascii")
        response = await self.client.chat.completions.create(
            model=self.config.vision_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"{user_text}\n請先理解照片，再用人設自然回覆；"
                                "只回最終文字，不要描述你的分析過程。"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{encoded}"
                            },
                        },
                    ],
                },
            ],
            temperature=self.config.ai_temperature,
            max_tokens=self.config.ai_max_tokens,
            extra_headers={"X-Include-Cost": "true"},
            timeout=self.config.ai_timeout,
        )
        content = response.choices[0].message.content
        return str(content or "").strip()

    async def _download(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("媒體下載網址不安全")
        chunks: list[bytes] = []
        total = 0
        async with self.http.stream("GET", url) as response:
            response.raise_for_status()
            length = int(response.headers.get("Content-Length") or 0)
            if length > self._MAX_OUTPUT_BYTES:
                raise ValueError("生成媒體大小不合規")
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._MAX_OUTPUT_BYTES:
                    raise ValueError("生成媒體大小不合規")
                chunks.append(bytes(chunk))
        data = b"".join(chunks)
        if not data:
            raise ValueError("生成媒體為空")
        return data

    async def generate_image(
        self, account_id: str, prompt: str
    ) -> MediaAsset | None:
        if not await self._reserve(account_id, "image"):
            return None
        response = await self.client.images.generate(
            model=self.config.image_model,
            prompt=self._SUGGESTIVE_IMAGE_POLICY + prompt,
            n=1,
            size="1024x1024",
            quality="standard",
            response_format="b64_json",
            timeout=self.config.media_generation_timeout,
        )
        item = response.data[0]
        if getattr(item, "b64_json", None):
            data = base64.b64decode(item.b64_json, validate=True)
        elif getattr(item, "url", None):
            data = await self._download(str(item.url))
        else:
            raise ValueError("圖片生成未返回內容")
        if not data or len(data) > 10 * 1024 * 1024:
            raise ValueError("生成圖片大小不合規")
        return MediaAsset("image", data, "image.png", "image/png")

    async def generate_voice(
        self, account_id: str, text: str, *, voice: str = "nova"
    ) -> MediaAsset | None:
        if not await self._reserve(account_id, "voice"):
            return None
        response = await self.client.audio.speech.create(
            model=self.config.speech_model,
            input=text[:500],
            voice=voice,
            response_format="opus",
            speed=1.0,
            timeout=self.config.media_generation_timeout,
        )
        data = bytes(await response.aread())
        if not data or len(data) > self._MAX_OUTPUT_BYTES:
            raise ValueError("生成語音大小不合規")
        return MediaAsset("voice", data, "voice.ogg", "audio/ogg")

    async def generate_video(
        self, account_id: str, prompt: str
    ) -> MediaAsset | None:
        if not await self._reserve(account_id, "video"):
            return None
        base = str(self.config.ai_base_url).rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.config.ai_api_key}",
            "Content-Type": "application/json",
        }
        response = await self.http.post(
            f"{base}/video/generations",
            headers=headers,
            json={
                "model": self.config.video_model,
                "prompt": self._SUGGESTIVE_VIDEO_POLICY + prompt,
                "duration": 4,
                "size": "768P",
                "metadata": {"ratio": "9:16"},
            },
        )
        response.raise_for_status()
        payload = response.json()
        task_id = str(payload.get("task_id") or payload.get("id") or "")
        if not task_id:
            raise ValueError("影片生成未返回 task_id")

        timeout = float(self.config.media_generation_timeout)
        elapsed = 0.0
        while elapsed < timeout:
            status_response = await self.http.get(
                f"{base}/video/generations/{task_id}", headers=headers
            )
            status_response.raise_for_status()
            status_payload = status_response.json()
            data = status_payload.get("data") or status_payload
            status = str(data.get("status") or "").upper()
            if status in {"SUCCESS", "COMPLETED"}:
                result_url = str(
                    data.get("result_url") or data.get("url") or ""
                )
                if not result_url:
                    raise ValueError("影片完成但沒有下載網址")
                content = await self._download(result_url)
                return MediaAsset("video", content, "video.mp4", "video/mp4")
            if status in {"FAILURE", "FAILED", "CANCELLED"}:
                reason = str(data.get("fail_reason") or "生成失敗")
                raise RuntimeError(reason)
            await self._sleep(5)
            elapsed += 5
        raise TimeoutError("影片生成逾時")
