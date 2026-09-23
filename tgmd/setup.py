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
both are restricted to admins. That restriction is not decoration: a bot that
asks a stranger for their Telegram login code is the shape of the most common
account-theft scam on the platform, and this wizard is only legitimate because
the person running it owns both the bot and the account. The prompts say so.
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

        lines = ["<b>Setup</b>", ""]
        lines.append("✅ <b>Bot account</b> — connected, you are talking to it")

        if reading:
            source = "from the environment" if self.session_pinned_by_env else (
                "from an in-chat login" if stored else "from a session file"
            )
            lines.append(f"✅ <b>Reading account</b> — connected {source}")
        else:
            lines.append(
                "⬜ <b>Reading account</b> — needed for private and "
                "save-restricted chats\n"
                "    <code>/setup telegram</code> to sign in here"
            )

        if pikpak_ready:
            which = "your own account" if pikpak_own else "the shared account"
            lines.append(f"✅ <b>PikPak</b> — {which}")
        else:
            lines.append(
                "⬜ <b>PikPak</b> — optional, for cloud transfers\n"
                "    <code>/setup pikpak</code> to sign in here"
            )

        if self._config.delivery.cache_chat_id:
            lines.append("✅ <b>Upload cache</b> — configured")
        else:
            lines.append(
                "⬜ <b>Upload cache</b> — optional. Add me to a private channel "
                "as admin, then set <code>CACHE_CHAT_ID</code> to its id."
            )

        lines.append("")
        if reading:
            lines.append("You can start sending links. /help lists what I take.")
        else:
            lines.append(
                "Public channels already work. Private ones need the reading "
                "account."
            )
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
            await event.reply(
                "The operator has disabled per-user PikPak logins."
            )
            return

        alternative = ""
        if self._portal.unavailable_reason() is None:
            alternative = (
                "\n\nPrefer a web form? <code>/pikpak login</code> sends a "
                "one-time link instead."
            )

        await self.cancel(event.sender_id)
        self._conversations[event.sender_id] = Conversation(
            user_id=event.sender_id, step=Step.PIKPAK_EMAIL
        )
        await event.reply(
            "<b>Connect PikPak</b>\n\n"
            "Send me the email or phone number your PikPak account uses."
            f"{alternative}\n\n"
            "I delete each message as soon as I have read it, and only the "
            "access token is stored, never your password.\n"
            "Send any other command to stop.",
            parse_mode="html",
        )

    async def begin_telegram(self, event) -> None:
        """Start the Telegram user-session conversation."""
        if self.session_pinned_by_env:
            await event.reply(
                "<code>TG_USER_SESSION</code> is set in the environment, so a "
                "login here would be ignored. Remove it first, or keep using "
                "the session you have.",
                parse_mode="html",
            )
            return
        if not event.is_private:
            await event.reply("Message me directly to sign in, not in a group.")
            return

        await self.cancel(event.sender_id)
        self._conversations[event.sender_id] = Conversation(
            user_id=event.sender_id, step=Step.TG_PHONE
        )
        await event.reply(
            "<b>Connect a reading account</b>\n\n"
            "This signs a normal Telegram account in to me, so I can read "
            "chats a bot cannot: private channels you are in, and channels "
            "that block saving.\n\n"
            "⚠️ I am about to ask for a login code. That is only safe because "
            "<b>you run this bot yourself</b>. Never give a Telegram login "
            "code to a bot or person you do not operate.\n\n"
            "Send the phone number of the account to use, with its country "
            "code, like <code>+8613800138000</code>.\n"
            "Send any other command to stop.",
            parse_mode="html",
        )

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
            await event.reply("That setup step timed out. Start again when ready.")
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
                f"❌ That did not work: {escape_html(str(exc))}\nStart again when ready.",
                parse_mode="html",
            )
        return True

    async def _forget(self, event) -> None:
        """Delete a message that contained a secret."""
        try:
            await event.delete()
        except Exception:
            log.debug("could not delete a secret message", exc_info=True)
            await event.reply(
                "I could not delete that message; please delete it yourself."
            )

    # ----------------------------------------------------------------- PikPak

    async def _pikpak_email(self, event, conversation: Conversation, text: str) -> None:
        if "@" not in text and not text.lstrip("+").isdigit():
            await event.reply("That does not look like an email or phone number.")
            return
        conversation.email = text
        conversation.step = Step.PIKPAK_PASSWORD
        await self._forget(event)
        await event.respond(
            f"Account: <code>{escape_html(text)}</code>\n\n"
            "Now send the password. I will delete it immediately.",
            parse_mode="html",
        )

    async def _pikpak_password(
        self, event, conversation: Conversation, text: str
    ) -> None:
        await self._forget(event)
        notice = await event.respond("Signing in to PikPak…")
        try:
            account = await self._pikpak.login_with_password(
                conversation.user_id, conversation.email or "", text
            )
        except PikPakError as exc:
            conversation.step = Step.PIKPAK_PASSWORD
            await notice.edit(
                f"❌ {escape_html(str(exc))}\n\nSend the password again, or any "
                "command to stop.",
                parse_mode="html",
            )
            return

        await self.cancel(conversation.user_id)
        await notice.edit(
            f"✅ PikPak connected as <code>{escape_html(account)}</code>.\n\n"
            "Your transfers now go to your own drive. "
            "<code>/pikpak</code> shows quota, <code>/pikpak logout</code> "
            "disconnects it.",
            parse_mode="html",
        )

    # --------------------------------------------------------------- Telegram

    async def _telegram_phone(
        self, event, conversation: Conversation, text: str
    ) -> None:
        phone = text.replace(" ", "").replace("-", "")
        if not phone.lstrip("+").isdigit() or len(phone.lstrip("+")) < 6:
            await event.reply(
                "That does not look like a phone number. Include the country "
                "code, like <code>+8613800138000</code>.",
                parse_mode="html",
            )
            return

        client = TelegramClient(
            StringSession(),
            self._config.telegram.api_id,
            self._config.telegram.api_hash,
        )
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            await client.disconnect()
            await event.reply("Telegram says that phone number is not valid.")
            return
        except FloodWaitError as exc:
            await client.disconnect()
            await self.cancel(conversation.user_id)
            await event.reply(
                f"Telegram is rate-limiting logins for {exc.seconds}s. Try later."
            )
            return

        conversation.phone = phone
        conversation.phone_code_hash = sent.phone_code_hash
        conversation.client = client
        conversation.step = Step.TG_CODE
        await self._forget(event)
        await event.respond(
            "Telegram is sending a login code to that account, in the Telegram "
            "app itself.\n\n"
            "Send me the code. Put a space or dash between the digits if "
            "Telegram refuses to let you copy it, for example "
            "<code>1 2 3 4 5</code>. I delete it immediately.",
            parse_mode="html",
        )

    async def _telegram_code(
        self, event, conversation: Conversation, text: str
    ) -> None:
        code = "".join(character for character in text if character.isdigit())
        if not code:
            await event.reply("I could not find any digits in that.")
            return

        client = conversation.client
        if client is None:  # pragma: no cover - defensive
            await self.cancel(conversation.user_id)
            await event.reply("That login expired. Start again with /setup telegram.")
            return

        await self._forget(event)
        notice = await event.respond("Signing in…")
        try:
            await client.sign_in(
                phone=conversation.phone,
                code=code,
                phone_code_hash=conversation.phone_code_hash,
            )
        except SessionPasswordNeededError:
            conversation.step = Step.TG_PASSWORD
            await notice.edit(
                "That account has two-step verification. Send its password; "
                "I delete it immediately and never store it."
            )
            return
        except PhoneCodeInvalidError:
            await notice.edit("That code is wrong. Send it again.")
            return
        except PhoneCodeExpiredError:
            await self.cancel(conversation.user_id)
            await notice.edit(
                "That code expired. Start again with /setup telegram."
            )
            return

        await self._finish_telegram(conversation, notice)

    async def _telegram_password(
        self, event, conversation: Conversation, text: str
    ) -> None:
        client = conversation.client
        if client is None:  # pragma: no cover - defensive
            await self.cancel(conversation.user_id)
            await event.reply("That login expired. Start again with /setup telegram.")
            return

        await self._forget(event)
        notice = await event.respond("Checking the password…")
        try:
            await client.sign_in(password=text)
        except Exception as exc:
            await notice.edit(
                f"❌ {escape_html(str(exc))}\n\nSend the password again, or any "
                "command to stop.",
                parse_mode="html",
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
            await notice.edit(
                f"✅ Signed in as <code>{escape_html(label)}</code> and saved.\n\n"
                "Restart the bot to start using it.",
                parse_mode="html",
            )
            return

        try:
            adopted = await self._adopt_session(session_string)
        except Exception as exc:
            log.exception("could not adopt the new user session")
            await notice.edit(
                f"✅ Signed in as <code>{escape_html(label)}</code> and saved, "
                f"but could not bring it into service now ({escape_html(str(exc))}). "
                "Restart the bot.",
                parse_mode="html",
            )
            return

        await notice.edit(
            f"✅ Reading account connected: <code>{escape_html(adopted)}</code>\n\n"
            "Private and save-restricted chats work now, with no restart. "
            "Send me a link to try it.\n\n"
            "The session is stored on this server and is as sensitive as the "
            "account password. Revoke it any time from Telegram → Settings → "
            "Devices.",
            parse_mode="html",
        )


async def stored_user_session(db: Database) -> str | None:
    """Return the session saved by an in-chat login, if any."""
    return await db.kv_get(USER_SESSION_KEY)
