from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx


MODELS_URL = "https://openrouter.ai/api/v1/models/user"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_MODELS = 2_000


class OpenRouterModelCatalogError(RuntimeError):
    """A safe, operator-facing model catalogue error."""


@dataclass(frozen=True)
class OpenRouterTextModel:
    id: str
    name: str


class OpenRouterModelCatalog:
    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        cache_ttl_seconds: float = 300,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cached: tuple[OpenRouterTextModel, ...] = ()
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def list_models(
        self, *, force_refresh: bool = False
    ) -> tuple[tuple[OpenRouterTextModel, ...], bool]:
        now = time.monotonic()
        if not force_refresh and self._cached_at and now - self._cached_at < self._cache_ttl_seconds:
            return self._cached, True
        async with self._lock:
            now = time.monotonic()
            if not force_refresh and self._cached_at and now - self._cached_at < self._cache_ttl_seconds:
                return self._cached, True
            models = await self._fetch()
            self._cached = models
            self._cached_at = time.monotonic()
            return models, False

    async def _fetch(self) -> tuple[OpenRouterTextModel, ...]:
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0),
            follow_redirects=False,
        )
        try:
            try:
                async with asyncio.timeout(10):
                    async with client.stream(
                        "GET",
                        MODELS_URL,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Accept": "application/json",
                        },
                    ) as response:
                        if response.status_code in (401, 403):
                            raise OpenRouterModelCatalogError("OpenRouter API Key 無權讀取模型清單")
                        if response.status_code == 429:
                            raise OpenRouterModelCatalogError("OpenRouter 模型清單請求過於頻繁，請稍後重試")
                        if response.status_code != 200:
                            raise OpenRouterModelCatalogError("OpenRouter 模型清單目前無法取得")
                        content_type = response.headers.get("content-type", "").lower()
                        if "application/json" not in content_type:
                            raise OpenRouterModelCatalogError("OpenRouter 模型清單回應格式不正確")
                        raw_length = response.headers.get("content-length")
                        if raw_length:
                            try:
                                if int(raw_length) > MAX_RESPONSE_BYTES:
                                    raise OpenRouterModelCatalogError("OpenRouter 模型清單回應過大")
                            except ValueError as exc:
                                raise OpenRouterModelCatalogError("OpenRouter 模型清單回應格式不正確") from exc
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_RESPONSE_BYTES:
                                raise OpenRouterModelCatalogError("OpenRouter 模型清單回應過大")
            except OpenRouterModelCatalogError:
                raise
            except (TimeoutError, httpx.TimeoutException):
                raise OpenRouterModelCatalogError("OpenRouter 模型清單請求逾時") from None
            except httpx.HTTPError:
                raise OpenRouterModelCatalogError("OpenRouter 模型清單目前無法取得") from None
        finally:
            if own_client:
                await client.aclose()
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise OpenRouterModelCatalogError("OpenRouter 模型清單回應格式不正確") from None
        return self._parse(payload)

    @staticmethod
    def _parse(payload: object) -> tuple[OpenRouterTextModel, ...]:
        if not isinstance(payload, dict) or set(payload).isdisjoint({"data"}):
            raise OpenRouterModelCatalogError("OpenRouter 模型清單回應格式不正確")
        data = payload.get("data")
        if not isinstance(data, list) or len(data) > MAX_MODELS:
            raise OpenRouterModelCatalogError("OpenRouter 模型清單回應格式不正確")
        models: dict[str, OpenRouterTextModel] = {}
        for item in data:
            if not isinstance(item, dict):
                raise OpenRouterModelCatalogError("OpenRouter 模型清單回應格式不正確")
            model_id = item.get("id")
            name = item.get("name")
            architecture = item.get("architecture")
            if (
                not isinstance(model_id, str)
                or not model_id.strip()
                or len(model_id) > 200
                or any(ord(char) < 32 for char in model_id)
                or not isinstance(name, str)
                or not name.strip()
                or len(name) > 300
                or any(ord(char) < 32 for char in name)
                or not isinstance(architecture, dict)
            ):
                raise OpenRouterModelCatalogError("OpenRouter 模型清單回應格式不正確")
            modalities = architecture.get("output_modalities")
            if not isinstance(modalities, list) or not all(
                isinstance(value, str) for value in modalities
            ):
                raise OpenRouterModelCatalogError("OpenRouter 模型清單回應格式不正確")
            if "text" in modalities:
                clean_id = model_id.strip()
                models[clean_id] = OpenRouterTextModel(clean_id, name.strip())
        return tuple(models[key] for key in sorted(models, key=str.casefold))
