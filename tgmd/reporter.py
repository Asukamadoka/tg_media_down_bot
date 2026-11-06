"""Progress messages, edited in place and throttled to avoid flood waits."""

from __future__ import annotations

import asyncio
import logging
import time

from telethon import TelegramClient
from telethon.errors import FloodWaitError, MessageNotModifiedError

log = logging.getLogger(__name__)


class Reporter:
    """Owns one status message in a chat and keeps it up to date.

    Telegram rate-limits message edits hard, so updates are collapsed to one
    every ``interval`` seconds. The final state is always written, even if it
    lands inside the throttle window.
    """

    def __init__(
        self,
        bot: TelegramClient,
        chat_id: int,
        *,
        interval: float = 5.0,
        reply_to: int | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._interval = interval
        self._reply_to = reply_to
        self._message = None
        self._last_text = ""
        self._last_edit = 0.0
        self._lock = asyncio.Lock()

    async def open(self, text: str) -> None:
        """Post the initial status message."""
        async with self._lock:
            try:
                self._message = await self._bot.send_message(
                    self._chat_id,
                    text,
                    parse_mode="html",
                    reply_to=self._reply_to,
                    link_preview=False,
                )
                self._last_text = text
                self._last_edit = time.monotonic()
            except Exception:
                log.exception("could not post a status message")

    async def update(self, text: str, *, force: bool = False) -> None:
        """Edit the status message, unless it was edited too recently."""
        if self._message is None:
            return
        now = time.monotonic()
        if not force and now - self._last_edit < self._interval:
            return
        if text == self._last_text:
            return
        async with self._lock:
            self._last_edit = now
            try:
                await self._message.edit(text, parse_mode="html", link_preview=False)
                self._last_text = text
            except MessageNotModifiedError:
                self._last_text = text
            except FloodWaitError as exc:
                # Back off for real: pushing through here gets the bot limited.
                log.info("edit flood wait %ss, pausing progress updates", exc.seconds)
                self._last_edit = now + exc.seconds
            except Exception as exc:  # noqa: BLE001 - progress is cosmetic, the job is not
                log.debug("status edit failed: %s", exc)

    async def close(self, text: str) -> None:
        """Write the final state, bypassing the throttle."""
        await self.update(text, force=True)
