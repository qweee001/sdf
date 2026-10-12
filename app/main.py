from __future__ import annotations

import asyncio
import signal
import sys

import uvicorn

from .bot import make_bot
from .config import load_settings
from .crypto import SecretBox
from .dashboard import Dashboard
from .database import Database
from .manager import AccountManager
from .telegram_login import TelegramLoginService


async def async_main() -> None:
    print("SDF 啟動中…", flush=True)
    settings = load_settings()
    print(f"設定載入完成（port={settings.dashboard_port}）", flush=True)

    secret_box = SecretBox(settings.account_encryption_key)
    db = Database(settings.db_path)
    await db.connect()
    print("資料庫連線完成", flush=True)

    manager = AccountManager(settings, db, secret_box)
    await manager.load_runtime_settings()
    login_service = TelegramLoginService(settings.tg_api_id, settings.tg_api_hash)
    dashboard = Dashboard(settings, manager, login_service)
    print("控制台初始化完成", flush=True)

    # Telegram 控制台 bot 要先建（物件建立不連 TG），讓 Mini App 路由能在
    # uvicorn startup 前掛到 dashboard.app；run() 的 task 仍在 uvicorn 後再開。
    # 注意：dashboard 在 __init__ 就 app.mount("/", StaticFiles) 掛根路徑，
    # include_router 會把 bot 路由排到 mount「後面」而被它先吃掉（GET 404 /
    # POST 405）。所以掛完再把它們的 routes 移到最前面。
    bot = make_bot(settings, manager, login_service)
    if bot is not None:
        # #11：/health 要含 bot 狀態（防 health 3/3 但 bot 死）
        dashboard.attach_bot(bot)
        _app = dashboard.app
        _routes = _app.router.routes
        _n = len(_routes)
        _app.include_router(bot.app)
        # FastAPI 1.x 用 lazy _IncludedRouter：include 後 routes 尾端多出一個物件。
        # 把該物件移到最前面，讓 /tg* 先於 dashboard 的根 static mount 匹配。
        if len(_routes) > _n:
            _inc = _routes.pop()
            _routes.insert(0, _inc)
        print("Mini App 路由已掛到控制台（前置於 static mount）", flush=True)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    # 先啟動控制台（讓 /health 盡快可達）
    config = uvicorn.Config(
        dashboard.app,
        host="0.0.0.0",
        port=settings.dashboard_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    print(f"控制台已啟動 0.0.0.0:{settings.dashboard_port}", flush=True)

    # 再開水軍帳號（部署重啟後自動恢復）
    try:
        await manager.start_all()
        print(f"水軍帳號啟動完成（{len(manager.workers)} 個運行中）", flush=True)
    except Exception as e:
        print(f"啟動水軍帳號失敗：{e}", flush=True)

    # Telegram 控制台 bot（有 BOT_TOKEN 才啟動；獨立 task，掛了不影響主流程）
    bot_task: asyncio.Task | None = None
    if bot is not None:
        def _bot_task_done(t: "asyncio.Task") -> None:
            # #11：done 監督——task 意外結束（run() 迴圈自己退出）要重新拉起，
            # 否則 bot 靜默失能而 /health 仍 3/3。正常 shutdown 不重啟。
            nonlocal bot_task
            if stop_event.is_set() or t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                print(
                    f"⚠️ bot 任務結束：{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            print("bot 任務已結束，3s 後重新拉起…", flush=True)

            def _relaunch() -> None:
                nonlocal bot_task
                if stop_event.is_set():
                    return
                bot_task = asyncio.create_task(bot.run())
                bot_task.add_done_callback(_bot_task_done)
                print("Telegram 控制台 bot 已重新拉起", flush=True)

            loop.call_later(3, _relaunch)

        bot_task = asyncio.create_task(bot.run())
        print("Telegram 控制台 bot 任務已建立", flush=True)
        bot_task.add_done_callback(_bot_task_done)

    # 同時監看停止訊號與控制台服務：若 serve() 先結束（例如連接埠被占用而立即拋錯），
    # 不能只等 stop_event，否則容器會活著但 HTTP 完全不可用。
    stop_task = asyncio.create_task(stop_event.wait())
    done, _ = await asyncio.wait(
        {server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    server_failure: BaseException | None = None
    if server_task in done and not server_task.cancelled():
        # 讀出例外（含 SystemExit 之類的 BaseException），避免 never retrieved 警告。
        server_failure = server_task.exception()
        if server_failure is not None:
            print(
                f"控制台服務結束：{type(server_failure).__name__}: {server_failure}",
                file=sys.stderr,
                flush=True,
            )
    if not stop_task.done():
        stop_task.cancel()
    await asyncio.gather(stop_task, return_exceptions=True)

    if server_failure is not None:
        print("控制台已停止，正在關閉…", flush=True)
    else:
        print("收到停止訊號，正在關閉…", flush=True)
    server.should_exit = True
    await manager.aclose()
    if bot is not None:
        bot.stop()  # 先告訴 run() 的重試迴圈「正常停止」，避免 done 監督又拉起
    if bot_task is not None:
        bot_task.cancel()
        try:
            await bot_task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        await server_task
    except asyncio.CancelledError:
        raise
    except BaseException as e:
        # 控制台已先結束：這裡不讓它中斷關閉流程，DB 仍必須關閉。
        if server_failure is None:
            server_failure = e
    await db.close()
    print("已關閉", flush=True)
    if server_failure is not None:
        # 控制台非正常結束：讓行程以非零碼退出，否則 railway.json 的
        # restartPolicyType=ON_FAILURE 不會把容器拉起來（容器活著但 HTTP 全廢）。
        raise server_failure


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"啟動失敗：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
