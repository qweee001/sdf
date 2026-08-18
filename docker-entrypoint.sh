#!/bin/sh
set -e
# Railway 掛載的 /data 卷是 root 擁有；降權到 app 前先把它改成 app 可寫，
# 否則 aiosqlite 會 "unable to open database file"。
if [ "$(id -u)" = "0" ]; then
    chown -R app:app /data 2>/dev/null || true
    chown -R app:app /app 2>/dev/null || true
    exec gosu app "$@"
else
    exec "$@"
fi
