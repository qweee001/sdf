from __future__ import annotations

import asyncio
import logging
import signal
import sys

from asyncio import CancelledError
from typing import Any

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


# ── Railway health-check endpoint ──────────────────────────
_health_app = FastAPI()


@_health_app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ── main ───────────────────────────────────────────────────
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

    # 1) 启动 health server (port 8000 – Railway checks /health here)
    import uvicorn

    health_config = uvicorn.Config(
        _health_app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",
    )
    health_server = uvicorn.Server(health_config)
    health_task = asyncio.create_task(health_server.serve())

    # 2) 启动 manager
    await manager.start()

    # 3) 启动 dashboard (port 8001)
    if settings.dashboard_enabled:
        dashboard = DashboardServer(
            username=settings.dashboard_username,
            password=settings.dashboard_password,
            port=8001,
            manager=manager,
        )
        await dashboard.start()
        LOGGER.info("Multi-account dashboard listening on port 8001")

    status: dict[str, Any] = await manager.status()
    LOGGER.info(
        "Multi-account manager started: total=%s enabled=%s connected=%s memory_ttl=%sh",
        int(status["summary"]["total"]),
        int(status["summary"]["enabled"]),
        int(status["summary"]["connected"]),
        settings.memory_ttl_hours,
    )

    await stop_event.wait()

    # shutdown
    if dashboard is not None:
        await dashboard.close()
    health_server.should_exit = True
    try:
        await asyncio.wait_for(health_task, timeout=3)
    except (asyncio.TimeoutError, CancelledError):
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
