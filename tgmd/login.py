"""Interactive helper that produces a user session string.

A bot account cannot read the history of a chat it is not in, and cannot read
chats that restrict saving content at all. So downloads run through a real
account, and this script is how you authorise one:

    python -m tgmd.login

Paste the printed string into ``TG_USER_SESSION``. It is a credential: anyone
holding it has full access to the account, so treat it like a password.
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


def _prompt(label: str, env_var: str) -> str:
    value = os.environ.get(env_var, "").strip()
    if value:
        return value
    return input(f"{label}: ").strip()


async def generate() -> int:
    load_dotenv()

    raw_api_id = _prompt("API ID (from https://my.telegram.org/apps)", "TG_API_ID")
    try:
        api_id = int(raw_api_id)
    except ValueError:
        print("API ID must be a number", file=sys.stderr)
        return 2

    api_hash = _prompt("API hash", "TG_API_HASH")
    if not api_hash:
        print("API hash is required", file=sys.stderr)
        return 2

    print(
        "\nSigning in. Telegram will send a login code to the account.\n"
        "This authorises a user account, not the bot.\n"
    )

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = await client.get_me()
        session_string = client.session.save()
        name = f"@{me.username}" if me.username else me.first_name
        print(f"\nSigned in as {name} (id {me.id})\n")
        print("Add this line to your .env file:\n")
        print(f"TG_USER_SESSION={session_string}\n")
        print(
            "Keep it secret — it grants full access to the account. "
            "Revoke it from Telegram → Settings → Devices if it leaks."
        )
    return 0


def main() -> int:
    try:
        return asyncio.run(generate())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
