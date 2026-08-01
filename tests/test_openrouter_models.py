from __future__ import annotations

import unittest

import httpx

from app.openrouter_models import (
    MAX_RESPONSE_BYTES,
    OpenRouterModelCatalog,
    OpenRouterModelCatalogError,
)


class OpenRouterModelCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_text_models_sanitizes_and_caches(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertEqual(request.headers["authorization"], "Bearer railway-secret")
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "data": [
                        {
                            "id": "vendor/text-model",
                            "name": "Text model",
                            "architecture": {"output_modalities": ["text"]},
                            "provider_secret": "must-not-leave-server",
                        },
                        {
                            "id": "vendor/image-model",
                            "name": "Image model",
                            "architecture": {"output_modalities": ["image"]},
                        },
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            catalog = OpenRouterModelCatalog("railway-secret", client=client)
            first, cached = await catalog.list_models()
            second, cached_second = await catalog.list_models()
        self.assertFalse(cached)
        self.assertTrue(cached_second)
        self.assertEqual(first, second)
        self.assertEqual([(model.id, model.name) for model in first], [("vendor/text-model", "Text model")])
        self.assertEqual(calls, 1)
        self.assertNotIn("railway-secret", repr(first))
        self.assertNotIn("must-not-leave-server", repr(first))

    async def test_refresh_bypasses_cache(self) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"data": [{"id": f"model-{calls}", "name": "Model", "architecture": {"output_modalities": ["text"]}}]},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            catalog = OpenRouterModelCatalog("secret", client=client)
            await catalog.list_models()
            refreshed, cached = await catalog.list_models(force_refresh=True)
        self.assertFalse(cached)
        self.assertEqual(refreshed[0].id, "model-2")

    async def test_rejects_oversized_and_malformed_responses(self) -> None:
        responses = [
            httpx.Response(200, headers={"content-type": "application/json", "content-length": str(MAX_RESPONSE_BYTES + 1)}, content=b"{}"),
            httpx.Response(200, headers={"content-type": "application/json"}, json={"data": "not-a-list"}),
        ]
        for response in responses:
            with self.subTest(response=response):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
                    with self.assertRaises(OpenRouterModelCatalogError):
                        await OpenRouterModelCatalog("secret", client=client).list_models()

    async def test_provider_error_does_not_expose_key_or_body(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(401, text="railway-secret provider detail")
            )
        ) as client:
            with self.assertRaises(OpenRouterModelCatalogError) as caught:
                await OpenRouterModelCatalog("railway-secret", client=client).list_models()
        self.assertNotIn("railway-secret", str(caught.exception))
        self.assertNotIn("provider detail", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
