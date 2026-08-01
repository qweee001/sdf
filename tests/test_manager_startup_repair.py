from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.manager import AccountManager


class ManagerStartupRepairTests(unittest.TestCase):
    def test_retired_model_repair_runs_after_legacy_import(self) -> None:
        async def scenario() -> None:
            calls: list[str] = []
            manager = AccountManager.__new__(AccountManager)
            manager.settings = SimpleNamespace(
                migrate_existing_accounts_to_grok_adult=False,
                ai_base_url="https://api.example.com/v1",
                media_uses_openrouter=False,
            )
            manager.store = SimpleNamespace(
                open=AsyncMock(side_effect=lambda: calls.append("open")),
                repair_retired_grok_model=AsyncMock(
                    side_effect=lambda: calls.append("repair")
                ),
                clear_account_api_keys=AsyncMock(return_value=0),
                list_accounts=AsyncMock(return_value=[]),
            )
            manager._import_legacy_account = AsyncMock(  # type: ignore[method-assign]
                side_effect=lambda: calls.append("import")
            )
            manager._refresh_managed_ids = AsyncMock()  # type: ignore[method-assign]
            manager._cleanup_loop = AsyncMock(return_value=None)  # type: ignore[method-assign]
            manager._supervisor_loop = AsyncMock(return_value=None)  # type: ignore[method-assign]

            await manager.start()
            await asyncio.gather(
                manager._cleanup_task,
                manager._supervisor_task,
            )

            self.assertEqual(calls[:3], ["open", "import", "repair"])
            manager.store.repair_retired_grok_model.assert_awaited_once()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
