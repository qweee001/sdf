#!/bin/sh
set -e
# Railway 掛載的 /data 卷是 root 擁有；降權到 app 前先把它改成 app 可寫，
# 否則 aiosqlite 會 "unable to open database file"。
if [ "$(id -u)" = "0" ]; then
    # 只需要修 /data（Railway 掛載的卷是 root 擁有），不然 aiosqlite 會
    # "unable to open database file"。應用代碼在鏡像裡已經是 app:app，
    # 每次啟動再 chown -R /app 只是白白拖慢啟動。
    chown -R app:app /data 2>/dev/null || true
    exec gosu app "$@"
else
    exec "$@"
fi
