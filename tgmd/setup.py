"""The in-Telegram setup wizard.

Three credentials have to reach the bot before it can do anything: the API id
and hash, the bot token, and a user session for reading chat history. The
first two must be in the environment before the process starts, because the
bot cannot talk to Telegram without them. Everything after that can be
finished from inside the Telegram app, which is what this module is for.

It handles two conversations:

* **PikPak** — email, then password, exchanged once for a token.
* **Telegram user session** — phone number, then the login code, then the
  two-step password if the account has one. The resulting session is stored
  and swapped into the running bot, so there is no restart.

Both delete the messages containing secrets as soon as they are consumed, and
both only run in a private chat. The Telegram one is restricted to admins,
and that restriction is not decoration: a bot that asks a stranger for their
Telegram login code is the shape of the most common account-theft scam on the
platform, and this wizard is only legitimate because the person running it
owns both the bot and the account. The prompts say so. The PikPak one is open
to any allowed user, because it connects their own drive and nobody else's,
exactly as the Mini App does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from .config import Config
from .db import Database
from .i18n import describe, t
from .identity import describe_account
from .pikpak import PikPakError, PikPakService
from .portal import PikPakLoginPortal
from .utils import escape_html

log = logging.getLogger(__name__)

USER_SESSION_KEY = "user_session_string"
"""Database key holding a session created by an in-chat login."""

# A half-finished wizard is dropped after this long, so an abandoned login
# does not leave a connected client lying around.
SESSION_TIMEOUT = 600.0

AdoptSession = Callable[[str], Awaitable[str]]
"""Called with a fresh session string; returns a description of the account."""


class Step(Enum):
    """Where a conversation currently is."""

    PIKPAK_EMAIL = "pikpak_email"
    PIKPAK_PASSWORD = "pikpak_password"
    TG_PHONE = "tg_phone"
    TG_CODE = "tg_code"
    TG_PASSWORD = "tg_password"


@dataclass
class Conversation:
    """In-memory state for one admin's wizard run.

    Deliberately not persisted: it holds a phone number, a half-authenticated
    client and, briefly, a password.
    """

    user_id: int
    step: Step
    started_at: float = field(default_factory=time.monotonic)
    email: str | None = None
    phone: str | None = None
    phone_code_hash: str | None = None
    client: TelegramClient | None = None

    @property
    def stale(self) -> bool:
        return time.monotonic() - self.started_at > SESSION_TIMEOUT

    async def close(self) -> None:
        """Drop the half-authenticated client, if there is one."""
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # pragma: no cover - best effort
                log.debug("could not disconnect a wizard client", exc_info=True)
            self.client = None


class SetupWizard:
    """Drives the credential conversations and reports what is still missing."""

    def __init__(
        self,
        bot: TelegramClient,
        config: Config,
        db: Database,
        pikpak: PikPakService,
        portal: PikPakLoginPortal,
        *,
        adopt_session: AdoptSession | None = None,
        has_user_client: Callable[[], bool] = lambda: False,
    ) -> None:
        self._bot = bot
        self._config = config
        self._db = db
        self._pikpak = pikpak
        self._portal = portal
        self._adopt_session = adopt_session
        self._has_user_client = has_user_client
        self._conversations: dict[int, Conversation] = {}
        # The event loop holds only weak references to tasks, so a
        # fire-and-forget close could be garbage-collected before it runs.
        self._background: set[asyncio.Task] = set()

    # ----------------------------------------------------------------- status

    @property
    def session_pinned_by_env(self) -> bool:
        """True when TG_USER_SESSION is set, which overrides an in-chat login."""
        return bool(self._config.telegram.user_session)

    async def status_text(self, user_id: int) -> str:
        """A checklist of what is done and what is left."""
        reading = self._has_user_client()
        stored = bool(await self._db.kv_get(USER_SESSION_KEY))
        pikpak_ready = await self._pikpak.available_for(user_id)
        pikpak_own = await self._pikpak.has_user_session(user_id)

        lines = [t("setup.status.title"), ""]
        lines.append(t("setup.status.bot"))

        if reading:
            source = t("setup.status.source_env") if self.session_pinned_by_env else (
                t("setup.status.source_chat") if stored else t("setup.status.source_file")
            )
            lines.append(t("setup.status.reading_ok", source=source))
        else:
            lines.append(t("setup.status.reading_missing"))

        if pikpak_ready:
            which = t("setup.status.pikpak_own") if pikpak_own else t("setup.status.pikpak_shared")
            lines.append(t("setup.status.pikpak_ok", which=which))
        else:
            lines.append(t("setup.status.pikpak_missing"))

        if self._config.delivery.cache_chat_id:
            lines.append(t("setup.status.cache_ok"))
        else:
            lines.append(t("setup.status.cache_missing"))

        lines.append("")
        lines.append(t("setup.status.ready") if reading else t("setup.status.public_only"))
        return "\n".join(lines)

    # ------------------------------------------------------------- entrypoints

    def active(self, user_id: int) -> bool:
        """True when this user is mid-conversation."""
        conversation = self._conversations.get(user_id)
        if conversation is None:
            return False
        if conversation.stale:
            # Detach now, synchronously. Closing its client needs an await and
            # so runs later; if that later step popped by user id instead, it
            # would close a fresh conversation the user started in between.
            del self._conversations[user_id]
            log.info("setup conversation for %s expired", user_id)
            self._close_in_background(conversation)
            return False
        return True

    def _close_in_background(self, conversation: Conversation) -> None:
        """Close a detached conversation's client without blocking the caller."""
        task = asyncio.create_task(conversation.close())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _expire(self, user_id: int) -> None:
        conversation = self._conversations.pop(user_id, None)
        if conversation is not None:
            await conversation.close()
            log.info("setup conversation for %s expired", user_id)

    async def cancel(self, user_id: int) -> bool:
        """Abandon a conversation. Returns True if one was running."""
        conversation = self._conversations.pop(user_id, None)
        if conversation is None:
            return False
        await conversation.close()
        return True

    async def begin_pikpak(self, event) -> None:
        """Start the PikPak conversation, offering the web page as well."""
        if not self._pikpak.user_login_allowed:
            await event.reply(t("setup.pikpak.disabled"))
            return

        # The password comes next, and it does not belong in a group.
        if not event.is_private:
            await event.reply(t("setup.private_only"))
            return

        alternative = ""
        if self._portal.miniapp_url is not None:
            alternative = t("setup.pikpak.form")

        await self.cancel(event.sender_id)
        self._conversations[event.sender_id] = Conversation(
            user_id=event.sender_id, step=Step.PIKPAK_EMAIL
        )
        await event.reply(t("setup.pikpak.start", alternative=alternative), parse_mode="html")

    async def begin_telegram(self, event) -> None:
        """Start the Telegram user-session conversation."""
        if self.session_pinned_by_env:
            await event.reply(t("setup.telegram.pinned"), parse_mode="html")
            return
        if not event.is_private:
            await event.reply(t("setup.private_only"))
            return

        await self.cancel(event.sender_id)
        self._conversations[event.sender_id] = Conversation(
            user_id=event.sender_id, step=Step.TG_PHONE
        )
        await event.reply(t("setup.telegram.start"), parse_mode="html")

    # --------------------------------------------------------------- dispatch

    async def handle(self, event) -> bool:
        """Consume a message if it belongs to a conversation.

        Returns True when the message was part of the wizard, so the caller
        knows not to treat it as a link.
        """
        user_id = event.sender_id
        conversation = self._conversations.get(user_id)
        if conversation is None:
            return False
        if conversation.stale:
            await self._expire(user_id)
            await event.reply(t("setup.timeout"))
            return True

        text = (event.raw_text or "").strip()
        # Any command aborts, so a user is never trapped in the wizard.
        if text.startswith("/"):
            await self.cancel(user_id)
            return False

        try:
            if conversation.step is Step.PIKPAK_EMAIL:
                await self._pikpak_email(event, conversation, text)
            elif conversation.step is Step.PIKPAK_PASSWORD:
                await self._pikpak_password(event, conversation, text)
            elif conversation.step is Step.TG_PHONE:
                await self._telegram_phone(event, conversation, text)
            elif conversation.step is Step.TG_CODE:
                await self._telegram_code(event, conversation, text)
            elif conversation.step is Step.TG_PASSWORD:
                await self._telegram_password(event, conversation, text)
        except Exception as exc:
            log.exception("setup step %s failed", conversation.step)
            await self.cancel(user_id)
            await event.reply(
                t("setup.failed", error=escape_html(describe(exc))), parse_mode="html"
            )
        return True

    async def _forget(self, event) -> None:
        """Delete a message that contained a secret."""
        try:
            await event.delete()
        except Exception:
            log.debug("could not delete a secret message", exc_info=True)
            await event.reply(t("setup.cannot_delete"))

    # ----------------------------------------------------------------- PikPak

    async def _pikpak_email(self, event, conversation: Conversation, text: str) -> None:
        if "@" not in text and not text.lstrip("+").isdigit():
            await event.reply(t("setup.pikpak.bad_account"))
            return
        conversation.email = text
        conversation.step = Step.PIKPAK_PASSWORD
        await self._forget(event)
        await event.respond(
            t("setup.pikpak.ask_password", account=escape_html(text)), parse_mode="html"
        )

    async def _pikpak_password(
        self, event, conversation: Conversation, text: str
    ) -> None:
        await self._forget(event)
        notice = await event.respond(t("setup.pikpak.signing_in"))
        try:
            account = await self._pikpak.login_with_password(
                conversation.user_id, conversation.email or "", text
            )
        except PikPakError as exc:
            conversation.step = Step.PIKPAK_PASSWORD
            await notice.edit(
                t("setup.retry_password", error=escape_html(describe(exc))), parse_mode="html"
            )
            return

        await self.cancel(conversation.user_id)
        await notice.edit(
            t("setup.pikpak.connected", account=escape_html(account)), parse_mode="html"
        )

    # --------------------------------------------------------------- Telegram

    async def _telegram_phone(
        self, event, conversation: Conversation, text: str
    ) -> None:
        phone = text.replace(" ", "").replace("-", "")
        if not phone.lstrip("+").isdigit() or len(phone.lstrip("+")) < 6:
            await event.reply(t("setup.telegram.bad_phone"), parse_mode="html")
            return

        client = TelegramClient(
            StringSession(),
            self._config.telegram.api_id,
            self._config.telegram.api_hash,
        )
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            await client.disconnect()
            await event.reply(t("setup.telegram.phone_invalid"))
            return
        except FloodWaitError as exc:
            await client.disconnect()
            await self.cancel(conversation.user_id)
            await event.reply(t("setup.telegram.flood", seconds=exc.seconds))
            return
        except BaseException:
            # Not attached to the conversation yet, so cancel() cannot reach
            # it: disconnect here or it stays connected.
            await client.disconnect()
            raise

        conversation.phone = phone
        conversation.phone_code_hash = sent.phone_code_hash
        conversation.client = client
        conversation.step = Step.TG_CODE
        await self._forget(event)
        await event.respond(t("setup.telegram.code_sent"), parse_mode="html")

    async def _telegram_code(
        self, event, conversation: Conversation, text: str
    ) -> None:
        code = "".join(character for character in text if character.isdigit())
        if not code:
            await event.reply(t("setup.telegram.no_digits"))
            return

        client = conversation.client
        if client is None:  # pragma: no cover - defensive
            await self.cancel(conversation.user_id)
            await event.reply(t("setup.telegram.expired"))
            return

        await self._forget(event)
        notice = await event.respond(t("setup.telegram.signing_in"))
        try:
            await client.sign_in(
                phone=conversation.phone,
                code=code,
                phone_code_hash=conversation.phone_code_hash,
            )
        except SessionPasswordNeededError:
            conversation.step = Step.TG_PASSWORD
            await notice.edit(t("setup.telegram.two_step"))
            return
        except PhoneCodeInvalidError:
            await notice.edit(t("setup.telegram.code_wrong"))
            return
        except PhoneCodeExpiredError:
            await self.cancel(conversation.user_id)
            await notice.edit(t("setup.telegram.code_expired"))
            return

        await self._finish_telegram(conversation, notice)

    async def _telegram_password(
        self, event, conversation: Conversation, text: str
    ) -> None:
        client = conversation.client
        if client is None:  # pragma: no cover - defensive
            await self.cancel(conversation.user_id)
            await event.reply(t("setup.telegram.expired"))
            return

        await self._forget(event)
        notice = await event.respond(t("setup.telegram.checking"))
        try:
            await client.sign_in(password=text)
        except Exception as exc:  # noqa: BLE001 - any failure means "try again"
            await notice.edit(
                t("setup.retry_password", error=escape_html(describe(exc))), parse_mode="html"
            )
            return

        await self._finish_telegram(conversation, notice)

    async def _finish_telegram(self, conversation: Conversation, notice) -> None:
        """Store the new session and bring it into service."""
        client = conversation.client
        assert client is not None

        account = await client.get_me()
        session_string = client.session.save()
        # The live client is rebuilt from the string by the adopter, so this
        # one is no longer needed and must not linger connected.
        conversation.client = None
        await client.disconnect()
        await self.cancel(conversation.user_id)

        await self._db.kv_set(USER_SESSION_KEY, session_string)

        label = describe_account(account)
        if self._adopt_session is None:
            await notice.edit(t("setup.telegram.saved", label=escape_html(label)),
                              parse_mode="html")
            return

        try:
            adopted = await self._adopt_session(session_string)
        except Exception as exc:
            log.exception("could not adopt the new user session")
            await notice.edit(
                t("setup.telegram.not_adopted", label=escape_html(label),
                  error=escape_html(describe(exc))),
                parse_mode="html",
            )
            return

        await notice.edit(t("setup.telegram.connected", account=escape_html(adopted)),
                          parse_mode="html")


async def stored_user_session(db: Database) -> str | None:
    """Return the session saved by an in-chat login, if any."""
    return await db.kv_get(USER_SESSION_KEY)
