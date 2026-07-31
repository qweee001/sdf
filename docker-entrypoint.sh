#!/bin/sh
set -eu

db_path="${MEMORY_DB_PATH:-/data/memory.db}"

case "$db_path" in
    /data/*) ;;
    *)
        echo "MEMORY_DB_PATH must point inside /data" >&2
        exit 1
        ;;
esac

db_dir="$(dirname -- "$db_path")"
mkdir -p "$db_dir"
chown app:app "$db_dir"

for db_file in "$db_path" "$db_path-wal" "$db_path-shm"; do
    if [ -e "$db_file" ]; then
        chown app:app "$db_file"
    fi
done

exec gosu app "$@"
