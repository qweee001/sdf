"""端到端驗證：控制台 serve() 一啟動就失敗時，行程是否以非零碼退出。

Railway 的 railway.json 是 restartPolicyType=ON_FAILURE —— 只有非零退出才會被拉起。
執行：見 run_probe_exit.sh
"""
import asyncio
import sys

import uvicorn

from app import main as main_mod


async def _boom(self, *args, **kwargs):
    raise OSError("probe: 連接埠被佔用")


uvicorn.Server.serve = _boom


def run():
    try:
        main_mod.main()
    except SystemExit as exc:
        print(f"PROBE: SystemExit code = {exc.code}")
        raise
    print("PROBE: main() 正常返回（沒有非零退出）")


if __name__ == "__main__":
    run()
