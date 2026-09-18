"""Self-configuration: the bot sets up its own BotFather-facing details.

Telegram exposes the command menu and the profile texts over the API, so the
bot writes them itself at startup instead of asking its operator to paste a
block into @BotFather. That removes the two chores people most often skip,
and keeps the menu honest: the list a user sees is generated from the same
place the handlers are registered, so it cannot drift.

Privacy mode is the one thing that is genuinely BotFather-only, with no API
for it. :data:`MANUAL_STEPS` names what is left so setup instructions can be
generated rather than remembered.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient
from telethon.tl.functions.bots import SetBotCommandsRequest, SetBotInfoRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

log = logging.getLogger(__name__)

# Shown in the "/" menu inside Telegram, in this order. Descriptions have to
# fit on one line on a phone, so they are terse by necessity.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("help", "What I take and what I can do"),
    ("claim", "Become the admin of a freshly deployed bot"),
    ("setup", "Finish setup: sign in an account or PikPak"),
    ("cache", "Use a channel as the upload cache"),
    ("mode", "Where files go: telegram, local or pikpak"),
    ("status", "What I am working on"),
    ("cancel", "Stop one job, or all of them"),
    ("stats", "Your recent jobs"),
    ("pikpak", "PikPak account, quota and folder"),
    ("verify", "Check my identity and configuration"),
    ("id", "Your Telegram user id"),
)

# Telegram's own limits. Exceeding either is rejected outright.
ABOUT_LIMIT = 120
DESCRIPTION_LIMIT = 512

ABOUT = (
    "Send me a Telegram message link and I fetch the media behind it, "
    "or transfer it to PikPak."
)

DESCRIPTION = (
    "Send me any Telegram message link and I fetch the media behind it, "
    "even from channels that block saving. I can send the file back to you, "
    "keep it on the server, or transfer it into PikPak. Magnet links, direct "
    "URLs and PikPak share links go straight to PikPak.\n\n"
    "Send /setup to finish signing in, or /help to see everything I take."
)

MANUAL_STEPS: tuple[tuple[str, str], ...] = (
    (
        "/setprivacy → Disable",
        "lets me see links posted in groups I am in. Skip it if you will only "
        "message me directly. There is no API for this setting, so it has to "
        "be done in @BotFather.",
    ),
)
"""Things only a human can do in @BotFather, with why each matters."""


def command_list() -> list[BotCommand]:
    """The menu as Telegram's own objects."""
    return [
        BotCommand(command=command, description=description)
        for command, description in COMMANDS
    ]


async def apply(client: TelegramClient) -> list[str]:
    """Write the command menu and profile texts. Returns what was set.

    Never raises: a flood wait or a revoked permission here is not worth
    blocking startup over, and everything it sets is cosmetic. Failures are
    logged and reported in the returned list.
    """
    applied: list[str] = []

    try:
        await client(
            SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="",
                commands=command_list(),
            )
        )
        applied.append(f"command menu ({len(COMMANDS)} commands)")
    except Exception as exc:
        log.warning("could not set the command menu: %s", exc)

    # Both texts are capped by Telegram; truncating loses less than being
    # rejected outright would.
    about = ABOUT[:ABOUT_LIMIT]
    description = DESCRIPTION[:DESCRIPTION_LIMIT]
    try:
        await client(
            SetBotInfoRequest(lang_code="", about=about, description=description)
        )
        applied.append("profile text")
    except Exception as exc:
        # A bot can only edit its own info, and only when BotFather has not
        # been used to lock it; neither is fatal here.
        log.info("could not set the profile text: %s", exc)

    if applied:
        log.info("self-configured: %s", ", ".join(applied))
    return applied
