"""The forward fast path: deliver a Telegram file without downloading it.

When the source allows forwarding, Telegram can copy a file between chats
server-side, so nothing passes through this machine. The awkward part is that
the bot and the reading account are two accounts, and file references are
per account: the bot cannot reuse a reference the reading account fetched.
So the reading account forwards the message into the cache channel, where
the bot then reads it with references of its own and re-sends the media to
the user. Re-sending the media, rather than forwarding again, keeps the
"Forwarded from" header off the user's copy, and the cache channel entry
means the next request for the same message is served from there directly.

When the bot itself is the reader (no reading account yet), the references
are already the bot's, so it re-sends the media straight away.

Anything short of success is an :class:`Outcome` the caller falls back from:
a restricted source, no cache channel, or an error. The fallback is the
clone path (download, then upload), in the manner of tdl's direct → clone.
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.errors import ChatForwardsRestrictedError, RPCError

from .config import Config
from .db import Database
from .delivery import CAPTION_LIMIT
from .downloader import MediaInfo

log = logging.getLogger(__name__)

# After the cache channel could not be resolved for the reading account, do
# not try again for this long. Each try can list every dialog of the user's
# own account, which is not something to repeat on every file.
RESOLVE_RETRY_AFTER = 600.0


class Outcome(enum.Enum):
    FORWARDED = "forwarded"
    """Delivered server-side; nothing was downloaded."""

    RESTRICTED = "restricted"
    """The message or its chat forbids forwarding; only a download works."""

    NO_CACHE = "no_cache"
    """Forwardable, but there is no cache channel to forward through."""

    FAILED = "failed"
    """Forwarding was attempted and did not work."""


@dataclass
class Attempt:
    outcome: Outcome
    detail: str = ""

    @property
    def delivered(self) -> bool:
        return self.outcome is Outcome.FORWARDED


async def resolve_peer(client: TelegramClient, chat_id: int):
    """Resolve ``chat_id`` for ``client``, listing dialogs once if it must.

    A session built from a string keeps no entity cache across restarts, so
    a bare channel id is unknown to it until something has shown it the
    channel. Listing dialogs does, for every chat the account is in.
    """
    try:
        return await client.get_input_entity(chat_id)
    except ValueError:
        async for _ in client.iter_dialogs():
            pass
        return await client.get_input_entity(chat_id)


def forwardable(message, chat) -> bool:
    """False when the message or the chat it lives in forbids forwarding.

    Either flag is enough: "restrict saving content" on a channel sets it on
    the chat, and a single protected message sets it on the message.
    """
    return not (getattr(message, "noforwards", False) or getattr(chat, "noforwards", False))


class Forwarder:
    """Tries the zero-byte path for one message at a time."""

    def __init__(
        self,
        bot: TelegramClient,
        config: Config,
        db: Database,
        *,
        reader: Callable[[], TelegramClient | None],
    ) -> None:
        self._bot = bot
        self._config = config
        self._db = db
        # A callable, not a client: /setup telegram can swap the reading
        # account at runtime, and this must follow it.
        self._reader = reader
        # The cache channel as the reading account sees it, per (account,
        # channel): the resolved peer, or the time resolving last failed.
        self._peers: dict[tuple[int, int], object] = {}
        self._failed_at: dict[tuple[int, int], float] = {}

    async def deliver(
        self,
        *,
        chat_id: int,
        message,
        source,
        caption: str,
        key: str,
        info: MediaInfo,
    ) -> Attempt:
        """Get ``message``'s media to ``chat_id`` without downloading it."""
        if not forwardable(message, source):
            return Attempt(Outcome.RESTRICTED)

        reader = self._reader()
        if reader is None:
            # The bot read this message itself, so its references are the
            # bot's own and can be sent on directly.
            return await self._send(chat_id, message.media, caption, "bot reference")

        cache_chat_id = self._config.delivery.cache_chat_id
        if cache_chat_id is None:
            return Attempt(Outcome.NO_CACHE)

        try:
            peer = await self._peer_for(reader, cache_chat_id)
            forwarded = await reader.forward_messages(peer, message.id, from_peer=source)
        except ChatForwardsRestrictedError:
            # The flags said yes but Telegram says no, which happens when a
            # chat changes its setting after the message was read.
            return Attempt(Outcome.RESTRICTED)
        except (RPCError, ValueError) as exc:
            # ValueError is Telethon failing to resolve the cache channel for
            # the reading account, which usually means it is not a member.
            log.info("forwarding message %s to the cache chat failed: %s", message.id, exc)
            return Attempt(Outcome.FAILED, str(exc))

        forwarded_id = getattr(forwarded, "id", None)
        try:
            # Channel message ids are shared by every account, so the bot can
            # look up the same id with references of its own.
            copy = await self._bot.get_messages(cache_chat_id, ids=forwarded_id)
        except (RPCError, ValueError) as exc:
            log.info("the bot cannot read the forwarded copy %s: %s", forwarded_id, exc)
            return Attempt(Outcome.FAILED, str(exc))
        if copy is None or not getattr(copy, "media", None):
            return Attempt(Outcome.FAILED, "the forwarded copy has no media the bot can see")

        attempt = await self._send(chat_id, copy.media, caption, "cache channel")
        if attempt.delivered:
            await self._db.cache_store(
                key, cache_chat_id, forwarded_id, info.file_name, info.size
            )
        return attempt

    async def _peer_for(self, reader: TelegramClient, cache_chat_id: int):
        key = (id(reader), cache_chat_id)
        if key in self._peers:
            return self._peers[key]
        failed = self._failed_at.get(key)
        if failed is not None and time.monotonic() - failed < RESOLVE_RETRY_AFTER:
            raise ValueError("the reading account cannot see the cache channel")
        try:
            peer = await resolve_peer(reader, cache_chat_id)
        except ValueError:
            self._failed_at[key] = time.monotonic()
            raise
        self._peers[key] = peer
        self._failed_at.pop(key, None)
        return peer

    async def _send(self, chat_id: int, media, caption: str, via: str) -> Attempt:
        try:
            await self._bot.send_file(
                chat_id, media, caption=caption[:CAPTION_LIMIT], parse_mode="html"
            )
        except ChatForwardsRestrictedError:
            return Attempt(Outcome.RESTRICTED)
        except (RPCError, ValueError) as exc:
            log.info("re-sending media via %s failed: %s", via, exc)
            return Attempt(Outcome.FAILED, str(exc))
        log.info("delivered media via %s, nothing downloaded", via)
        return Attempt(Outcome.FORWARDED)
