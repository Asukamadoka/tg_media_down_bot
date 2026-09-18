"""Runtime bootstrap: settings that can be established without a redeploy.

Two settings used to cost a whole extra deploy cycle each, because they are
values you can only learn *after* the bot is running:

* **Who the admin is.** The old flow was: deploy, send ``/id``, read the
  number back, put it in ``ADMIN_USER_IDS``, deploy again.
* **Which channel caches uploads.** Same shape: create the channel, hunt down
  its ``-100…`` id, set the variable, deploy again.

Both are now claimed at runtime and persisted, so ``ADMIN_USER_IDS`` is
optional and ``CACHE_CHAT_ID`` never has to be looked up by hand.

Claiming is guarded by a one-time code the bot prints to its own log at
startup. That is a deliberate choice over first-sender-wins: bot usernames
are searchable, so whoever found the bot first would otherwise own it. The
operator is already looking at the deploy log, so reading one line costs
nothing, and the code stops working the moment an admin exists.

Runtime values are merged into the live :class:`~tgmd.config.Config` at
startup rather than consulted separately. That keeps every existing
``config.access.is_admin(...)`` call synchronous and correct: this process is
the only writer, so the in-memory list and the database cannot disagree.
"""

from __future__ import annotations

import logging
import secrets

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

CLAIM_CODE_KEY = "admin_claim_code"
ADMIN_IDS_KEY = "runtime_admin_ids"
CACHE_CHAT_KEY = "runtime_cache_chat_id"

# Long enough that guessing is hopeless, short enough to retype from a log.
CLAIM_CODE_BYTES = 8


class ClaimError(RuntimeError):
    """A claim attempt was refused, with a reason meant for the user."""


def _as_ids(value: object) -> list[int]:
    """Coerce stored JSON into a list of ids, dropping anything unusable."""
    if not isinstance(value, list):
        return []
    ids: list[int] = []
    for item in value:
        text = str(item).strip()
        if text.lstrip("-").isdigit():
            number = int(text)
            if number not in ids:
                ids.append(number)
    return ids


async def runtime_admin_ids(db: Database) -> list[int]:
    """Admin ids added at runtime by a successful claim."""
    return _as_ids(await db.kv_get_json(ADMIN_IDS_KEY))


async def runtime_cache_chat_id(db: Database) -> int | None:
    """The cache channel chosen at runtime, if any."""
    stored = await db.kv_get(CACHE_CHAT_KEY)
    if stored is None:
        return None
    text = stored.strip()
    return int(text) if text.lstrip("-").isdigit() else None


async def load_runtime_settings(db: Database, config: Config) -> None:
    """Merge persisted runtime settings into the live configuration.

    Call this after the database is open and before handlers are registered,
    so an admin claimed on a previous run is an admin on this one.
    """
    for user_id in await runtime_admin_ids(db):
        if user_id not in config.access.admin_user_ids:
            config.access.admin_user_ids.append(user_id)
            log.info("restored runtime admin %s", user_id)

    # An explicit CACHE_CHAT_ID in the environment is the operator's decision
    # and outranks anything claimed in chat.
    if config.delivery.cache_chat_id is None:
        stored = await runtime_cache_chat_id(db)
        if stored is not None:
            config.delivery.cache_chat_id = stored
            log.info("restored runtime cache chat %s", stored)


async def ensure_claim_code(db: Database) -> str:
    """Return the claim code, generating one on first use."""
    existing = await db.kv_get(CLAIM_CODE_KEY)
    if existing:
        return existing
    code = secrets.token_urlsafe(CLAIM_CODE_BYTES)
    await db.kv_set(CLAIM_CODE_KEY, code)
    return code


async def claim_available(db: Database, config: Config) -> bool:
    """True when the bot still has no admin and can be claimed."""
    return not config.access.admin_user_ids


async def announce_claim(db: Database, config: Config, bot_username: str | None) -> str | None:
    """Log how to claim the bot, and return the code, when there is no admin.

    The log line is deliberately loud: it is the one thing an operator has to
    read to finish setup, and it appears right where they already are.
    """
    if not await claim_available(db, config):
        return None

    code = await ensure_claim_code(db)
    where = f"@{bot_username}" if bot_username else "the bot"
    log.warning(
        "\n"
        "%s\n"
        "  NO ADMIN YET. Open %s in Telegram and send:\n"
        "\n"
        "      /claim %s\n"
        "\n"
        "  That makes you the admin. No redeploy needed, and this code stops\n"
        "  working straight afterwards.\n"
        "%s",
        "=" * 68,
        where,
        code,
        "=" * 68,
    )
    return code


async def claim_admin(db: Database, config: Config, code: str, user_id: int) -> None:
    """Make ``user_id`` an admin if ``code`` matches. Raises on refusal."""
    if not await claim_available(db, config):
        raise ClaimError(
            "this bot already has an admin, so it cannot be claimed again"
        )

    expected = await db.kv_get(CLAIM_CODE_KEY)
    if not expected:
        raise ClaimError(
            "no claim code has been issued. Restart the bot and read the code "
            "from its log."
        )
    if not secrets.compare_digest(code.strip(), expected):
        raise ClaimError("that claim code is wrong")

    await add_runtime_admin(db, config, user_id)
    # Spent: a code that still worked afterwards would be a standing backdoor.
    await db.kv_delete(CLAIM_CODE_KEY)
    log.warning("user %s claimed this bot as its admin", user_id)


async def add_runtime_admin(db: Database, config: Config, user_id: int) -> None:
    """Persist an admin and apply it to the running configuration."""
    stored = await runtime_admin_ids(db)
    if user_id not in stored:
        stored.append(user_id)
        await db.kv_set_json(ADMIN_IDS_KEY, stored)
    if user_id not in config.access.admin_user_ids:
        config.access.admin_user_ids.append(user_id)


async def set_cache_chat(db: Database, config: Config, chat_id: int) -> None:
    """Persist the upload cache channel and apply it to the running config."""
    await db.kv_set(CACHE_CHAT_KEY, str(chat_id))
    config.delivery.cache_chat_id = chat_id
    log.info("upload cache chat set to %s", chat_id)


async def clear_cache_chat(db: Database, config: Config) -> None:
    """Forget the runtime cache channel."""
    await db.kv_delete(CACHE_CHAT_KEY)
    config.delivery.cache_chat_id = None
    log.info("upload cache chat cleared")
