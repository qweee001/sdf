from __future__ import annotations

import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession


def main() -> None:
    print("This creates a sensitive Telegram session string. Never post or commit it.")
    api_id = int(input("TG_API_ID: ").strip())
    api_hash = getpass.getpass("TG_API_HASH: ").strip()
    phone = input("Telegram phone number (international format): ").strip()

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        client.start(phone=phone)
        session_string = StringSession.save(client.session)

    print("\nTG_SESSION_STRING (store only in Railway Variables):")
    print(session_string)


if __name__ == "__main__":
    main()

