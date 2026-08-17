from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .config import load_settings
from .crypto import SecretBox
from .dashboard import DashboardServer
from .manager import AccountManager
from .memory import MemoryStore


LOGGER = logging.getLogger("telegram-ai-userbot")


class _BelowWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING


def configure_logging(level: int) -> None:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    standard_output = logging.StreamHandler(sys.stdout)
    standard_output.setLevel(level)
    standard_output.addFilter(_BelowWarningFilter())
    standard_output.setFormatter(formatter)

    error_output = logging.StreamHandler(sys.stderr)
    error_output.setLevel(max(level, logging.WARNING))
    error_output.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(standard_output)
    root.addHandler(error_output)


async def async_main() -> None:
    print("async_main: loading settings", flush=True)
    settings = load_settings()
    print("async_main: settings loaded, configuring logging", flush=True)
    configure_logging(
        getattr(logging, settings.log_level, logging.INFO)
    )

    print("async_main: creating store and manager", flush=True)
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

    print("async_main: starting dashboard", flush=True)
    # 1) 先启动 dashboard（port 8000），让 /health 立即可用
    dashboard_task: asyncio.Task | None = None
    if settings.dashboard_enabled:
        dashboard = DashboardServer(
            username=settings.dashboard_username,
            password=settings.dashboard_password,
            port=8000,
            manager=manager,
        )
        dashboard_task = asyncio.create_task(dashboard.start())
        # 等待 dashboard 启动（简单 sleep，避免同步阻塞事件循环）
        for _ in range(10):
            await asyncio.sleep(0.1)
        LOGGER.info("Dashboard started on port 8000")
        print("async_main: dashboard task created", flush=True)

    # 2) 启动 manager（可能在后台，不影响 /health）
    print("async_main: starting manager", flush=True)
    await manager.start()
    print("async_main: manager started", flush=True)

    summary = await manager.status()
    LOGGER.info(
        "Multi-account manager started: total=%s enabled=%s connected=%s memory_ttl=%sh",
        summary.get("summary", {}).get("total", 0),
        summary.get("summary", {}).get("enabled", 0),
        summary.get("summary", {}).get("connected", 0),
        settings.memory_ttl_hours,
    )

    await stop_event.wait()

    # shutdown
    if dashboard is not None:
        dashboard.server.should_exit = True
        if dashboard_task is not None:
            try:
                await asyncio.wait_for(dashboard_task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass
    await manager.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
    except Exception:
        configure_logging(logging.INFO)
        LOGGER.exception("Fatal startup error")
        raise


if __name__ == "__main__":
    main()
