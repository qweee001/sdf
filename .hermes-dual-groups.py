import asyncio
import hashlib
import json
import os
import sqlite3

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.crypto import SecretBox


async def main() -> None:
    conn = sqlite3.connect("file:/data/chat.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    account = conn.execute(
        "SELECT session_key, groups FROM accounts WHERE enabled = 1 ORDER BY created_at LIMIT 1"
    ).fetchone()
    if account is None:
        raise SystemExit("no enabled account")
    selected = {int(value) for value in json.loads(account["groups"] or "[]")}
    session = SecretBox(os.environ["ACCOUNT_ENCRYPTION_KEY"]).decrypt(account["session_key"])
    client = TelegramClient(
        StringSession(session),
        int(os.environ["TG_API_ID"]),
        os.environ["TG_API_HASH"],
    )
    await client.connect()
    try:
        if await client.get_me() is None:
            raise SystemExit("session invalid")
        titles = {}
        async for dialog in client.iter_dialogs():
            if dialog.id in selected:
                titles[dialog.id] = str(dialog.title or "")
        out = [
            {
                "group_tag": hashlib.sha256(str(group_id).encode()).hexdigest()[:8],
                "title": titles.get(group_id, "<not found>"),
            }
            for group_id in sorted(selected)
        ]
        print(json.dumps(out, ensure_ascii=False))
    finally:
        await client.disconnect()
        conn.close()


asyncio.run(main())
