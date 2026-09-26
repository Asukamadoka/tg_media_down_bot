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

from .i18n import DEFAULT_LANGUAGE, t

log = logging.getLogger(__name__)

# Shown in the "/" menu inside Telegram, in this order. Descriptions have to
# fit on one line on a phone, so they are terse by necessity.
# Command NAMES are never translated: Telegram requires ^[a-z0-9_]{1,32}$, and
# handlers.register() matches them literally. Only the descriptions move.
COMMAND_NAMES: tuple[str, ...] = (
    "help",
    "claim",
    "setup",
    "cache",
    "mode",
    "status",
    "cancel",
    "stats",
    "pikpak",
    "wms",
    "do",
    "verify",
    "id",
)


def commands(lang: str | None = None) -> tuple[tuple[str, str], ...]:
    """The menu, as (name, description) pairs in the requested language."""
    return tuple((name, t(f"menu.{name}", lang=lang)) for name in COMMAND_NAMES)


COMMANDS: tuple[tuple[str, str], ...] = commands(DEFAULT_LANGUAGE)
"""The English menu, kept as a module constant for callers that expect it."""

# Telegram's own limits. Exceeding either is rejected outright.
ABOUT_LIMIT = 120
DESCRIPTION_LIMIT = 512

ABOUT = t("profile.about", lang=DEFAULT_LANGUAGE)

DESCRIPTION = t("profile.description", lang=DEFAULT_LANGUAGE)


def manual_steps(lang: str | None = None) -> tuple[tuple[str, str], ...]:
    """Things only a human can do in @BotFather, with why each matters."""
    return (
        (
            t("manual.setprivacy.label", lang=lang),
            t("manual.setprivacy.why", lang=lang),
        ),
    )


MANUAL_STEPS: tuple[tuple[str, str], ...] = manual_steps(DEFAULT_LANGUAGE)
"""Things only a human can do in @BotFather, with why each matters."""


def command_list(lang: str | None = None) -> list[BotCommand]:
    """The menu as Telegram's own objects, in the requested language.

    ``lang`` defaults to whatever :mod:`tgmd.i18n` is set to, so the menu
    follows the rest of the bot rather than the viewer's client language: an
    operator who runs the bot in Chinese wants a Chinese menu for everyone.
    """
    return [
        BotCommand(command=command, description=description)
        for command, description in commands(lang)
    ]


async def apply(client: TelegramClient) -> list[str]:
    """Write the command menu and profile texts. Returns what was set.

    Never raises: a flood wait or a revoked permission here is not worth
    blocking startup over, and everything it sets is cosmetic. Failures are
    logged and reported in the returned list.
    """
    applied: list[str] = []

    # The empty lang_code is the menu every client falls back to, which is
    # what we want: the bot speaks one language, chosen by its operator.
    try:
        await client(
            SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="",
                commands=command_list(),
            )
        )
        applied.append(f"command menu ({len(COMMAND_NAMES)} commands)")
    except Exception as exc:  # noqa: BLE001 - cosmetic; must not block startup
        log.warning("could not set the command menu: %s", exc)

    # Both texts are capped by Telegram; truncating loses less than being
    # rejected outright would.
    about = t("profile.about")[:ABOUT_LIMIT]
    description = t("profile.description")[:DESCRIPTION_LIMIT]
    try:
        await client(
            SetBotInfoRequest(lang_code="", about=about, description=description)
        )
        applied.append("profile text")
    except Exception as exc:  # noqa: BLE001 - cosmetic; must not block startup
        # A bot can only edit its own info, and only when BotFather has not
        # been used to lock it; neither is fatal here.
        log.info("could not set the profile text: %s", exc)

    if applied:
        log.info("self-configured: %s", ", ".join(applied))
    return applied
