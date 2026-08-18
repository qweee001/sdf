#!/bin/sh
set -e
# 用 gosu 降權到非 root 使用者執行
if [ "$(id -u)" = "0" ]; then
    exec gosu app "$@"
else
    exec "$@"
fi
