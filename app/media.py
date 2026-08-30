from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx
from openai import APIConnectionError
from PIL import Image, ImageOps


@dataclass(frozen=True)
class MediaAsset:
    kind: str
    data: bytes
    filename: str
    mime_type: str


class OrcaMediaService:
    """OrcaRouter 多模態與媒體生成；付費生成先走共用預算闸門。"""

    _MODEL_BUDGETS = {
        "vision": ("vision_model", "gemini-3.5-flash-lite", 0.10),
        "image": (
            "image_model",
            "google/imagen-4.0-fast-generate-001",
            0.03,
        ),
        "video": ("video_model", "minimax/minimax-h3", 0.40),
    }
    _PRIMARY_IMAGE_MODEL = "google/imagen-4.0-fast-generate-001"
    _FALLBACK_IMAGE_MODEL = "google/imagen-4.0-generate-001"
    _APPROVED_IMAGE_MODELS = {
        "google/imagen-4.0-fast-generate-001": 0.03,
        "google/imagen-4.0-generate-001": 0.04,
    }
    _MAX_OUTPUT_BYTES = 50 * 1024 * 1024
    _VISION_MAX_JPEG_BYTES = 512 * 1024
    _VISION_HARD_MAX_INPUT_BYTES = 8 * 1024 * 1024
    _VISION_MAX_DIMENSION = 1600
    _VISION_MIN_DIMENSION = 800
    _VISION_MAX_PIXELS = 12_000_000
    _VISION_ALLOWED_FORMATS_ORDERED = ("JPEG", "PNG", "WEBP")
    _VISION_ALLOWED_FORMATS = frozenset(_VISION_ALLOWED_FORMATS_ORDERED)
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
        resolve_host: Callable[[str, int], Awaitable[list[str]]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.client = client
        self.db = db
        self.config = config
        self._owns_http = http_client is None
        self.http = http_client or httpx.AsyncClient(
            timeout=float(config.media_generation_timeout)
        )
        self._resolve_host = resolve_host or self._system_resolve_host
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def _reserve(self, account_id: str, kind: str) -> bool:
        if not bool(getattr(self.config, "media_enabled", False)):
            return False
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

    async def _reserve_image_model(self, account_id: str, model: str) -> bool:
        if not bool(getattr(self.config, "media_enabled", False)):
            return False
        reserve_usd = self._APPROVED_IMAGE_MODELS.get(model)
        if reserve_usd is None:
            raise ValueError(f"未核准的 image 模型：{model or '<empty>'}")
        return await self.db.reserve_media_budget(
            account_id,
            "image",
            reserve_usd,
            float(self.config.media_daily_budget_usd),
        )

    @staticmethod
    def _is_transient_image_error(exc: Exception) -> bool:
        if isinstance(
            exc,
            (
                TimeoutError,
                asyncio.TimeoutError,
                httpx.TimeoutException,
                APIConnectionError,
            ),
        ):
            return True
        status_code = getattr(exc, "status_code", None)
        return isinstance(status_code, int) and status_code >= 500

    def _host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        for pattern in getattr(self.config, "media_download_hosts", ()):
            pattern = str(pattern).lower().rstrip(".")
            if pattern.startswith("*."):
                suffix = pattern[1:]
                if host.endswith(suffix) and host != suffix[1:]:
                    return True
            elif host == pattern:
                return True
        return False

    def _validate_download_url(self, url: str) -> str:
        parsed = urlparse(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("媒體下載網址不安全") from exc
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or not self._host_allowed(host)
        ):
            raise ValueError("媒體下載網址不安全")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("媒體下載網址不安全")
        return url

    @staticmethod
    async def _system_resolve_host(host: str, port: int) -> list[str]:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
        return sorted({str(info[4][0]) for info in infos if info[4]})

    async def _validate_resolved_host(self, url: str) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        try:
            addresses = await self._resolve_host(host, parsed.port or 443)
        except (OSError, ValueError) as exc:
            raise ValueError("媒體下載網址不安全") from exc
        if not addresses:
            raise ValueError("媒體下載網址不安全")
        for raw_address in addresses:
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError as exc:
                raise ValueError("媒體下載網址不安全") from exc
            if not address.is_global:
                raise ValueError("媒體下載網址不安全")

    @classmethod
    def _validate_vision_image_header(cls, data: bytes) -> None:
        with Image.open(
            BytesIO(data), formats=cls._VISION_ALLOWED_FORMATS_ORDERED
        ) as source:
            if str(source.format or "").upper() not in cls._VISION_ALLOWED_FORMATS:
                raise ValueError("輸入圖片格式不合規")
            width, height = source.size
            if (
                width <= 0
                or height <= 0
                or width * height > cls._VISION_MAX_PIXELS
            ):
                raise ValueError("輸入圖片尺寸不合規")

    @classmethod
    def _normalize_vision_image(cls, data: bytes) -> bytes:
        cls._validate_vision_image_header(data)
        with Image.open(
            BytesIO(data), formats=cls._VISION_ALLOWED_FORMATS_ORDERED
        ) as source:
            width, height = source.size
            if (
                width <= 0
                or height <= 0
                or width * height > cls._VISION_MAX_PIXELS
            ):
                raise ValueError("輸入圖片尺寸不合規")
            source.load()
            oriented = ImageOps.exif_transpose(source)
            if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                normalized = Image.new("RGB", rgba.size, "white")
                normalized.paste(rgba, mask=rgba.getchannel("A"))
            else:
                normalized = oriented.convert("RGB")

        largest_dimension = max(normalized.size)
        if largest_dimension > cls._VISION_MAX_DIMENSION:
            scale = cls._VISION_MAX_DIMENSION / largest_dimension
            normalized = normalized.resize(
                (
                    max(1, round(normalized.width * scale)),
                    max(1, round(normalized.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )

        while True:
            for quality in (85, 75, 65, 55):
                output = BytesIO()
                normalized.save(
                    output,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )
                payload = output.getvalue()
                if len(payload) <= cls._VISION_MAX_JPEG_BYTES:
                    return payload

            largest_dimension = max(normalized.size)
            if largest_dimension <= cls._VISION_MIN_DIMENSION:
                raise ValueError("正規化圖片大小不合規")
            next_largest = max(
                cls._VISION_MIN_DIMENSION,
                round(largest_dimension * 0.8),
            )
            scale = next_largest / largest_dimension
            normalized = normalized.resize(
                (
                    max(1, round(normalized.width * scale)),
                    max(1, round(normalized.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )

    async def understand_image(
        self,
        account_id: str,
        image: bytes,
        mime_type: str,
        system_prompt: str,
        user_text: str,
    ) -> str:
        input_limit = min(
            int(getattr(self.config, "media_max_input_bytes", 8 * 1024 * 1024)),
            self._VISION_HARD_MAX_INPUT_BYTES,
        )
        if not image or len(image) > input_limit:
            return ""
        try:
            await asyncio.to_thread(self._validate_vision_image_header, image)
            normalized = await asyncio.to_thread(self._normalize_vision_image, image)
        except Exception:
            return ""
        if not await self._reserve(account_id, "vision"):
            return ""
        encoded = base64.b64encode(normalized).decode("ascii")
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
                                "url": f"data:image/jpeg;base64,{encoded}"
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
        url = self._validate_download_url(url)
        await self._validate_resolved_host(url)
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
        primary = str(self.config.image_model)
        fallback = str(getattr(self.config, "image_fallback_model", ""))
        for model in (primary, fallback):
            if model not in self._APPROVED_IMAGE_MODELS:
                raise ValueError(f"未核准的 image 模型：{model or '<empty>'}")
        if (
            primary != self._PRIMARY_IMAGE_MODEL
            or fallback != self._FALLBACK_IMAGE_MODEL
        ):
            raise ValueError("圖片主備模型角色錯誤")
        models = [primary, fallback]

        response = None
        for index, model in enumerate(models):
            if not await self._reserve_image_model(account_id, model):
                return None
            try:
                response = await self.client.images.generate(
                    model=model,
                    prompt=self._SUGGESTIVE_IMAGE_POLICY + prompt,
                    n=1,
                    size="1024x1024",
                    quality="standard",
                    response_format="b64_json",
                    timeout=self.config.media_generation_timeout,
                )
                break
            except Exception as exc:
                has_fallback = index + 1 < len(models)
                if not has_fallback or not self._is_transient_image_error(exc):
                    raise
        if response is None:
            raise RuntimeError("圖片生成沒有可用模型")
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
        # 語音只允許未來的本地克隆台灣腔服務；禁止回退 OrcaRouter TTS。
        return None

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
