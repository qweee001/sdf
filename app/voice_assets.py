from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .media import MediaAsset


class VoiceAssetLibrary:
    """Load per-account pre-generated voice clips from a local manifest."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self._accounts: dict = {}
        self.reload()

    def reload(self) -> None:
        """Re-read manifest so newly deployed assets take effect without restart."""
        manifest_path = self.root / "manifest.json"
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        accounts = loaded.get("accounts", {}) if isinstance(loaded, dict) else {}
        self._accounts = accounts if isinstance(accounts, dict) else {}

    def asset_for_day(self, account_id: str, day_index: int) -> MediaAsset | None:
        entry = self._accounts.get(str(account_id))
        if not isinstance(entry, dict):
            return None
        clips = entry.get("clips", [])
        if not isinstance(clips, list) or not clips:
            return None
        valid = [clip for clip in clips if isinstance(clip, dict) and clip.get("path")]
        if not valid:
            return None
        offset = int.from_bytes(
            hashlib.sha256(str(account_id).encode("utf-8")).digest()[:8], "big"
        )
        clip = valid[(int(day_index) + offset) % len(valid)]
        path = (self.root / str(clip["path"])).resolve()
        if not path.is_relative_to(self.root):
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not data:
            return None
        return MediaAsset("voice", data, path.name, "audio/ogg")
