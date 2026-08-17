from __future__ import annotations

import asyncio
import logging
import signal
import sys

from fastapi import FastAPI
from fastapi.responses import JSONResponse
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


# 主健康检查端点（独立于 manager/dashboard）
health_app = FastAPI()


@health_app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def async_main() -> None:
    settings = load_settings()
    configure_logging(
        getattr(logging, settings.log_level, logging.INFO)
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

    # 先启动 dashboard 监听 PORT（默认 8000）
    dashboard: DashboardServer | None = None
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

    status: dict = await manager.status()
    LOGGER.info(
        "Multi-account manager started: total=%s enabled=%s connected=%s memory_ttl=%sh",
        status["summary"]["total"],
        status["summary"]["enabled"],
        status["summary"]["connected"],
        settings.memory_ttl_hours,
    )

    await stop_event.wait()
    if dashboard is not None:
        await dashboard.close()
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
