"""Construction of the two MTProto clients the bot runs.

Two clients are needed, and for different reasons:

* The **user client** reads message history. A bot account cannot fetch an
  arbitrary message by id from a channel it is not in, and cannot read chats
  that restrict saving content, so downloads go through a real account.
* The **bot client** talks to users. Going through MTProto rather than the
  HTTP Bot API also raises the upload ceiling from 50 MiB to 2 GiB.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

from .config import Config
from .identity import BotTokenError, account_link, describe_account, parse_bot_token

log = logging.getLogger(__name__)

# Presented to Telegram; matching a real client avoids odd rate limiting.
_DEVICE = {
    "device_model": "tg-media-down-bot",
    "system_version": "linux",
    "app_version": "0.1.0",
}


def build_user_client(config: Config) -> TelegramClient | None:
    """Create the user client, or ``None`` when no user session is available."""
    telegram = config.telegram
    if telegram.user_session:
        session = StringSession(telegram.user_session)
    elif telegram.user_session_file.exists():
        session = str(telegram.user_session_file.with_suffix(""))
    else:
        return None

    return TelegramClient(
        session,
        telegram.api_id,
        telegram.api_hash,
        # Telethon retries FloodWait itself only below this threshold; longer
        # waits are raised so the queue can report them to the user.
        flood_sleep_threshold=60,
        **_DEVICE,
    )


def build_bot_client(config: Config) -> TelegramClient:
    """Create the bot client, which always uses an on-disk session file."""
    telegram = config.telegram
    telegram.session_dir.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(telegram.bot_session_file.with_suffix("")),
        telegram.api_id,
        telegram.api_hash,
        flood_sleep_threshold=60,
        **_DEVICE,
    )


async def start_clients(config: Config) -> tuple[TelegramClient, TelegramClient | None]:
    """Connect both clients and return ``(bot, user)``.

    The user client is optional: without it the bot still works for chats it is
    itself a member of, which is enough for some setups and worth not blocking
    startup over.
    """
    bot = build_bot_client(config)
    await bot.start(bot_token=config.telegram.bot_token)
    me = await bot.get_me()
    log.info(
        "bot client started as %s%s",
        describe_account(me),
        f" — {account_link(me)}" if account_link(me) else "",
    )

    # The digits before the colon in a bot token are the bot's own user id, so
    # a mismatch means the token and the account that answered disagree.
    try:
        token = parse_bot_token(config.telegram.bot_token)
    except BotTokenError as exc:
        log.warning("bot token looks malformed (%s) even though it worked", exc)
    else:
        if me.id != token.bot_id:
            log.error(
                "bot identity mismatch: the token names id %s but the account "
                "that answered is %s",
                token.bot_id,
                me.id,
            )

    user = build_user_client(config)
    if user is None:
        log.warning(
            "no user session: only chats the bot itself belongs to can be read"
        )
        return bot, None

    await user.connect()
    if not await user.is_user_authorized():
        log.error(
            "the user session is not authorized; run `python -m tgmd.login` to "
            "create a fresh one. Continuing without it."
        )
        await user.disconnect()
        return bot, None

    account = await user.get_me()
    log.info(
        "user client started as %s (id %s)",
        f"@{account.username}" if account.username else account.first_name,
        account.id,
    )
    return bot, user
