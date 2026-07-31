from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from app.config import _azure_speech_region
from app.media_types import (
    AccountMediaSettings,
    MediaFeatureSettings,
    clean_account_media_settings,
    safe_media_job_payload,
)


class AccountMediaValidationTests(unittest.TestCase):
    def test_azure_speech_region_rejects_url_injection(self) -> None:
        for value in ("eastasia", "a2", "central-us-2"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"AZURE_SPEECH_REGION": value},
            ):
                self.assertEqual(_azure_speech_region(), value)

        for value in (
            "EastAsia",
            "eastasia.azure.com/path",
            "eastasia?x=1",
            "a",
            "a" * 41,
        ):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"AZURE_SPEECH_REGION": value},
            ), self.assertRaises(ValueError):
                _azure_speech_region()

        with patch.dict(os.environ, {"AZURE_SPEECH_REGION": ""}):
            self.assertEqual(_azure_speech_region(), "")

    def test_media_defaults_are_disabled_and_public_without_credentials(self) -> None:
        settings = AccountMediaSettings()
        public = settings.public_dict()
        self.assertEqual(set(public), {"image", "voice", "video"})
        for feature in public.values():
            self.assertFalse(feature["enabled"])
            self.assertEqual(feature["daily_limit"], 0)
            self.assertEqual(feature["cooldown_seconds"], 0)
            self.assertEqual(feature["allowed_group_ids"], [])
        serialized = json.dumps(public)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("secret", serialized)

    def test_media_settings_are_normalized_deduplicated_and_partial(self) -> None:
        current = AccountMediaSettings(
            voice=MediaFeatureSettings(
                enabled=True,
                model="existing-model",
                voice="zh-TW-HsiaoChenNeural",
                daily_limit=20,
                cooldown_seconds=60,
                allowed_group_ids=frozenset({-1001}),
            )
        )
        cleaned = clean_account_media_settings(
            {
                "image": {
                    "enabled": True,
                    "model": "  ｇｐｔ－ｉｍａｇｅ－１  ",
                    "daily_limit": 8,
                    "cooldown_seconds": 120,
                    "allowed_group_ids": [-1002, -1002, -1001],
                },
                "voice": {"daily_limit": 12},
            },
            current=current,
        )
        self.assertEqual(cleaned.image.model, "gpt-image-1")
        self.assertEqual(cleaned.image.allowed_group_ids, frozenset({-1001, -1002}))
        self.assertTrue(cleaned.voice.enabled)
        self.assertEqual(cleaned.voice.model, "existing-model")
        self.assertEqual(cleaned.voice.daily_limit, 12)
        self.assertFalse(cleaned.video.enabled)

    def test_media_settings_reject_unsafe_or_malformed_values(self) -> None:
        invalid_values = [
            {"audio": {}},
            {"image": {"enabled": "true"}},
            {"image": {"daily_limit": True}},
            {"image": {"daily_limit": 1001}},
            {"image": {"cooldown_seconds": 604801}},
            {"image": {"allowed_group_ids": [-1001, True]}},
            {"image": {"model": "bad\u0000model"}},
            {"image": {"unexpected": "value"}},
        ]
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                clean_account_media_settings(value)

    def test_media_job_payload_cannot_persist_credentials(self) -> None:
        self.assertEqual(
            json.loads(
                safe_media_job_payload(
                    {
                        "prompt": "自然風景",
                        "model": "gpt-image-1",
                        "max_tokens": 200,
                    }
                )
            ),
            {
                "prompt": "自然風景",
                "model": "gpt-image-1",
                "max_tokens": 200,
            },
        )
        for forbidden in (
            {"api_key": "secret"},
            {"provider": {"authorization": "Bearer secret"}},
            {"session_string": "secret"},
        ):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValueError):
                safe_media_job_payload(forbidden)


if __name__ == "__main__":
    unittest.main()
