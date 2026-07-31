from __future__ import annotations

import asyncio
import logging
import signal

from .config import load_settings
from .crypto import SecretBox
from .dashboard import DashboardServer
from .manager import AccountManager
from .memory import MemoryStore


LOGGER = logging.getLogger("telegram-ai-userbot")


async def async_main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    secrets = SecretBox(settings.account_encryption_key)
    store = MemoryStore(settings.memory_db_path, settings.memory_ttl_hours)
    manager = AccountManager(settings, store, secrets)
    dashboard: DashboardServer | None = None
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await manager.start()
        if settings.dashboard_enabled:
            dashboard = DashboardServer(
                username=settings.dashboard_username,
                password=settings.dashboard_password,
                port=settings.dashboard_port,
                manager=manager,
            )
            await dashboard.start()
            LOGGER.info("Multi-account dashboard listening on port %s", settings.dashboard_port)
        status = await manager.status()
        LOGGER.info(
            "Multi-account manager started: total=%s enabled=%s connected=%s memory_ttl=%sh",
            status["summary"]["total"],
            status["summary"]["enabled"],
            status["summary"]["connected"],
            settings.memory_ttl_hours,
        )
        await stop_event.wait()
    finally:
        if dashboard is not None:
            await dashboard.close()
        await manager.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.basicConfig(level=logging.INFO)
        LOGGER.exception("Fatal startup error")
        raise


if __name__ == "__main__":
    main()
