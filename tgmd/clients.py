"""Construction of the two MTProto clients the bot runs.

Two clients are needed, and for different reasons:

* The **user client** reads message history. A bot account cannot fetch an
  arbitrary message by id from a channel it is not in, and cannot read chats
  that restrict saving content, so downloads go through a real account.
* The **bot client** talks to users. Going through MTProto rather than the
  HTTP Bot API also raises the upload ceiling from 50 MiB to 2 GiB.
"""

from __future__ import annotations

import contextlib
import logging

from telethon import TelegramClient
from telethon.errors import RPCError
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


def user_session_source(
    config: Config, stored_session: str | None = None
) -> tuple[object, str] | None:
    """Pick which user session to use, and say where it came from.

    Precedence is environment, then an in-chat login stored in the database,
    then a session file. The environment wins so that an operator who pins
    ``TG_USER_SESSION`` gets exactly that, and the setup wizard tells them
    when their in-chat login would be ignored because of it.
    """
    telegram = config.telegram
    if telegram.user_session:
        return StringSession(telegram.user_session), "TG_USER_SESSION"
    if stored_session:
        return StringSession(stored_session), "an in-chat login"
    if telegram.user_session_file.exists():
        return str(telegram.user_session_file.with_suffix("")), str(
            telegram.user_session_file
        )
    return None


def build_user_client(
    config: Config, stored_session: str | None = None
) -> TelegramClient | None:
    """Create the user client, or ``None`` when no user session is available."""
    chosen = user_session_source(config, stored_session)
    if chosen is None:
        return None
    session, _source = chosen

    return TelegramClient(
        session,
        config.telegram.api_id,
        config.telegram.api_hash,
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


async def start_clients(
    config: Config, stored_session: str | None = None
) -> tuple[TelegramClient, TelegramClient | None]:
    """Connect both clients and return ``(bot, user)``.

    The user client is optional: without it the bot still works for chats it is
    itself a member of, and the setup wizard can add one later without a
    restart, so this must never block startup.
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

    user = build_user_client(config, stored_session)
    if user is None:
        log.warning(
            "no user session yet: only chats the bot itself belongs to can be "
            "read. An admin can add one from Telegram with /setup telegram."
        )
        return bot, None

    try:
        await user.connect()
        authorized = await user.is_user_authorized()
    except RPCError as exc:
        # Telegram can revoke a session outright: AuthKeyDuplicatedError when
        # the same key was used from two IP addresses at once, or the account
        # ended the session on another device. Raising here crash-looped the
        # whole bot and took /setup telegram, the way to fix it, down too.
        log.error(
            "Telegram rejected the user session (%s); continuing without it. "
            "An admin can sign in again with /setup telegram.",
            type(exc).__name__,
        )
        with contextlib.suppress(Exception):
            await user.disconnect()
        return bot, None
    if not authorized:
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
