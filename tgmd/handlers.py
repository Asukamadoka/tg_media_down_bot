"""Bot commands and the link-dispatch handler."""

from __future__ import annotations

import logging

from telethon import TelegramClient, events

from . import bootstrap
from .buttons import url_button, webview_button
from .config import MODES, Config
from .db import Database
from .downloader import has_downloadable_media
from .links import LinkBundle, extract_links
from .pikpak import PikPakError, PikPakService
from .portal import PikPakLoginPortal, PortalError
from .tasks import Job, JobKind, JobQueue, QueueFull
from .utils import escape_html, human_duration, human_size, parse_id_list, truncate
from .verify import run_live_checks

log = logging.getLogger(__name__)

HELP = """<b>Telegram media downloader</b>

Send me a Telegram message link and I will fetch the media behind it — even
from channels that block saving, as long as the reading account is a member.

<b>Links I understand</b>
• <code>https://t.me/channel/123</code> — public channel or group
• <code>https://t.me/c/1234567890/123</code> — private chat
• <code>https://t.me/channel/12/123</code> — a forum topic
• <code>https://t.me/channel/100-120</code> — a range of messages
• <code>?single</code> to take one album item, <code>?comment=45</code> for a comment
• a magnet link or direct URL — handed straight to PikPak
• a PikPak share link — saved into your drive
• media sent or forwarded to me directly

<b>Commands</b>
/mode — where files should go: telegram, local or pikpak
/status — what I am working on
/cancel [id] — stop one job, or everything
/stats — your recent jobs
/pikpak — PikPak account, quota and target folder
/pikpak login — connect your own PikPak account
/id — your Telegram user id
/help — this message"""

ADMIN_HELP = (
    "\n/setup — finish setup here: sign in a reading account or PikPak"
    "\n/cache — use a channel as the upload cache"
    "\n/verify — check the bot's identity and configuration"
)

UNCLAIMED_HELP = """<b>This bot has no admin yet</b>

Whoever deployed me left a claim code in my startup log. Send it here:

<code>/claim &lt;code&gt;</code>

That makes you the admin, with no redeploy. Until then I refuse every
request, including yours."""


class BotHandlers:
    """Registers every bot event handler against a client."""

    def __init__(
        self,
        bot: TelegramClient,
        config: Config,
        db: Database,
        queue: JobQueue,
        pikpak: PikPakService,
        portal: PikPakLoginPortal,
        *,
        user_client=None,
    ) -> None:
        self._bot = bot
        self._config = config
        self._db = db
        self._queue = queue
        self._pikpak = pikpak
        self._portal = portal
        self._user_client = user_client
        self._wizard = None

    @property
    def _has_user_client(self) -> bool:
        return self._user_client is not None

    def attach_wizard(self, wizard) -> None:
        """Give the handlers the setup wizard, before registering."""
        self._wizard = wizard

    def set_user_client(self, client) -> None:
        """Adopt a reading client added at runtime by the setup wizard."""
        self._user_client = client

    def register(self) -> None:
        """Attach all handlers. Commands are matched before the link fallback."""
        add = self._bot.add_event_handler
        add(self.on_help, events.NewMessage(pattern=r"^/(start|help)\b"))
        add(self.on_claim, events.NewMessage(pattern=r"^/claim\b"))
        add(self.on_cache, events.NewMessage(pattern=r"^/cache\b"))
        add(self.on_setup, events.NewMessage(pattern=r"^/setup\b"))
        add(self.on_id, events.NewMessage(pattern=r"^/id\b"))
        add(self.on_mode, events.NewMessage(pattern=r"^/mode\b"))
        add(self.on_status, events.NewMessage(pattern=r"^/status\b"))
        add(self.on_cancel, events.NewMessage(pattern=r"^/cancel\b"))
        add(self.on_stats, events.NewMessage(pattern=r"^/stats\b"))
        add(self.on_pikpak, events.NewMessage(pattern=r"^/pikpak\b"))
        add(self.on_verify, events.NewMessage(pattern=r"^/verify\b"))
        add(self.on_message, events.NewMessage(incoming=True))

    # --------------------------------------------------------- access control

    async def _authorized(self, event, *, quiet: bool = False) -> bool:
        """Check the sender against the allow list, explaining any refusal.

        The explanation is only worth sending in a direct chat: replying to
        every message in a group the bot happens to be in would be noise.
        """
        user_id = event.sender_id
        if user_id is None:
            return False
        if self._config.access.is_allowed(user_id):
            return True
        log.info("refused user %s in chat %s", user_id, event.chat_id)
        if quiet or not event.is_private:
            return False

        # An unclaimed bot refuses everyone, including the person who just
        # deployed it, so say how to fix that rather than just saying no.
        if await bootstrap.claim_available(self._db, self._config):
            await event.reply(UNCLAIMED_HELP, parse_mode="html", link_preview=False)
            return False

        await event.reply(
            "You are not allowed to use this bot.\n\n"
            f"Your user id is <code>{user_id}</code>. An admin can add it to "
            "<code>ALLOWED_USER_IDS</code>.",
            parse_mode="html",
        )
        return False

    async def _mode_for(self, user_id: int) -> str:
        """The user's chosen destination, falling back to the configured default."""
        record = await self._db.get_user(user_id)
        if record and record.get("mode") in MODES:
            return str(record["mode"])
        return self._config.delivery.default_mode

    async def _pikpak_folder_for(self, user_id: int) -> str | None:
        record = await self._db.get_user(user_id)
        if record and record.get("pikpak_dir"):
            return str(record["pikpak_dir"])
        return None

    # ---------------------------------------------------------------- commands

    async def on_help(self, event) -> None:
        if not await self._authorized(event):
            return
        text = HELP
        if self._config.access.is_admin(event.sender_id):
            text += ADMIN_HELP
        if not self._has_user_client:
            text += (
                "\n\n⚠️ No reading account is connected yet, so private and "
                "save-restricted chats will not work."
            )
            if self._config.access.is_admin(event.sender_id):
                text += " Send <code>/setup</code> to finish that here."
        await event.reply(text, parse_mode="html", link_preview=False)

    async def on_id(self, event) -> None:
        await event.reply(
            f"Your user id: <code>{event.sender_id}</code>\n"
            f"This chat id: <code>{event.chat_id}</code>",
            parse_mode="html",
        )

    async def on_mode(self, event) -> None:
        if not await self._authorized(event):
            return
        parts = (event.raw_text or "").split()
        current = await self._mode_for(event.sender_id)

        if len(parts) < 2:
            await event.reply(
                f"Current mode: <b>{current}</b>\n\n"
                "<code>/mode telegram</code> — send the file back to you\n"
                "<code>/mode local</code> — keep it on the server's disk\n"
                "<code>/mode pikpak</code> — transfer it into PikPak",
                parse_mode="html",
            )
            return

        choice = parts[1].lower()
        if choice not in MODES:
            await event.reply(
                f"Unknown mode {escape_html(choice)}. Pick one of: "
                + ", ".join(MODES)
            )
            return
        if choice == "pikpak" and not await self._pikpak.available_for(event.sender_id):
            if self._portal.unavailable_reason() is None:
                await event.reply(
                    "No PikPak account is connected yet. Send "
                    "<code>/pikpak login</code> first and I will send you a "
                    "login link.",
                    parse_mode="html",
                )
            else:
                await event.reply(
                    "PikPak is not available on this server. Ask the operator "
                    "to set PIKPAK_USERNAME and PIKPAK_PASSWORD, or to enable "
                    "login links."
                )
            return

        await self._db.set_user_mode(event.sender_id, choice)
        await event.reply(f"Mode set to <b>{choice}</b>.", parse_mode="html")

    async def on_status(self, event) -> None:
        if not await self._authorized(event):
            return
        jobs = self._queue.snapshot(event.sender_id)
        if not jobs:
            await event.reply("Nothing in your queue.")
            return
        lines = [
            f"<code>#{job.id}</code> {job.state.value} — {escape_html(truncate(job.label, 60))}"
            for job in jobs
        ]
        await event.reply(
            f"<b>{len(jobs)} item(s) in your queue</b>\n" + "\n".join(lines),
            parse_mode="html",
        )

    async def on_cancel(self, event) -> None:
        if not await self._authorized(event):
            return
        parts = (event.raw_text or "").split()
        job_id: int | None = None
        if len(parts) >= 2 and parts[1].lstrip("#").isdigit():
            job_id = int(parts[1].lstrip("#"))

        cancelled = self._queue.cancel(event.sender_id, job_id)
        if not cancelled:
            await event.reply(
                "No matching job found." if job_id else "Nothing to cancel."
            )
            return
        ids = ", ".join(f"#{job.id}" for job in cancelled)
        await event.reply(f"Cancelling {ids}.")

    async def on_stats(self, event) -> None:
        if not await self._authorized(event):
            return
        stats = await self._db.user_stats(event.sender_id)
        recent = await self._db.recent_jobs(event.sender_id, limit=8)

        if not stats and not recent:
            await event.reply("You have not downloaded anything yet.")
            return

        totals = " · ".join(
            f"{status}: {values['count']}" for status, values in sorted(stats.items())
        )
        transferred = sum(values["bytes"] for values in stats.values())
        lines = [
            f"<b>Totals</b>\n{totals or 'none'}\nTransferred: {human_size(transferred)}"
        ]
        if recent:
            lines.append("\n<b>Recent</b>")
            for job in recent:
                label = job.get("file_name") or job.get("link") or ""
                lines.append(
                    f"<code>#{job['id']}</code> {job['status']} — "
                    f"{escape_html(truncate(str(label), 48))}"
                )
        await event.reply("\n".join(lines), parse_mode="html", link_preview=False)

    async def on_pikpak(self, event) -> None:
        if not await self._authorized(event):
            return
        parts = (event.raw_text or "").split(maxsplit=2)
        action = parts[1].lower() if len(parts) >= 2 else "status"

        if action == "login":
            await self._pikpak_login(event)
            return
        if action == "logout":
            await self._pikpak_logout(event, parts)
            return
        if action == "dir":
            await self._pikpak_dir(event, parts)
            return
        await self._pikpak_status(event)

    async def _pikpak_login(self, event) -> None:
        """Offer every way of connecting PikPak that this deployment supports."""
        if not self._pikpak.user_login_allowed:
            await event.reply("The operator has disabled per-user PikPak logins.")
            return

        already = await self._pikpak.has_user_session(event.sender_id)
        replacing = (
            "\n\nThis replaces the account you have connected now." if already else ""
        )

        # Best case: a Mini App, which opens inside Telegram and needs no link
        # at all, because Telegram signs the visitor's identity for us.
        miniapp = self._portal.miniapp_url
        if miniapp is not None:
            await event.reply(
                "<b>Connect your PikPak account</b>\n\n"
                "Tap below to open the form inside Telegram. There is no link "
                "to leak: Telegram tells me who you are.\n\n"
                "Your password goes to PikPak once, in exchange for an access "
                f"token. Only the token is stored.{replacing}",
                parse_mode="html",
                buttons=webview_button("🔐 Connect PikPak", miniapp),
                link_preview=False,
            )
            return

        reason = self._portal.unavailable_reason()
        if reason is None:
            try:
                link = self._portal.create_link(event.sender_id)
            except PortalError as exc:
                await event.reply(f"❌ {escape_html(str(exc))}", parse_mode="html")
                return
            ttl = human_duration(self._config.pikpak.login_link_ttl)
            await event.reply(
                "<b>Connect your PikPak account</b>\n\n"
                f"The link works once and expires in {ttl}. It opens a page "
                "served by this bot, not by PikPak. Your password is used once "
                f"to get an access token, and only the token is stored.{replacing}",
                parse_mode="html",
                buttons=url_button("🔐 Open the login page", link),
                link_preview=False,
            )
            return

        # No usable web server: the in-chat conversation still works, and
        # needs no public address or TLS at all.
        await event.reply(
            "<b>Connect your PikPak account</b>\n\n"
            "Send <code>/setup pikpak</code> and I will ask for your email and "
            "password here, deleting each message as I read it.\n\n"
            f"<i>A web form is not available: {escape_html(reason)}</i>{replacing}",
            parse_mode="html",
        )

    async def _pikpak_logout(self, event, parts: list[str]) -> None:
        """Clear the caller's own session, or the shared one for an admin."""
        scope = parts[2].strip().lower() if len(parts) >= 3 else ""
        if scope == "shared":
            if not self._config.access.is_admin(event.sender_id):
                await event.reply("Only an admin can clear the shared session.")
                return
            await self._pikpak.logout()
            await event.reply("Shared PikPak session cleared.")
            return

        if not await self._pikpak.has_user_session(event.sender_id):
            await event.reply("You have no PikPak account connected.")
            return
        await self._pikpak.logout(event.sender_id)
        self._portal.revoke(event.sender_id)
        await event.reply(
            "Your PikPak account is disconnected and the stored token is gone."
        )

    async def _pikpak_dir(self, event, parts: list[str]) -> None:
        if len(parts) < 3:
            current = await self._pikpak_folder_for(event.sender_id)
            await event.reply(
                "Your PikPak folder: "
                f"<code>{escape_html(current or self._config.pikpak.folder)}</code>\n"
                "Change it with <code>/pikpak dir /Movies/Anime</code>.",
                parse_mode="html",
            )
            return
        folder = "/" + parts[2].strip().strip("/")
        await self._db.set_user_pikpak_dir(event.sender_id, folder)
        await event.reply(
            f"PikPak folder set to <code>{escape_html(folder)}</code>.",
            parse_mode="html",
        )

    async def _pikpak_status(self, event) -> None:
        user_id = event.sender_id
        own = await self._pikpak.has_user_session(user_id)

        if not await self._pikpak.available_for(user_id):
            reason = self._portal.unavailable_reason()
            if reason is None:
                await event.reply(
                    "No PikPak account is connected. Send "
                    "<code>/pikpak login</code> and I will send you a login link.",
                    parse_mode="html",
                )
            else:
                await event.reply(
                    "PikPak is not available on this server.\n"
                    f"Login links: {escape_html(reason)}",
                    parse_mode="html",
                )
            return

        try:
            quota = await self._pikpak.quota(user_id=user_id)
        except PikPakError as exc:
            await event.reply(f"❌ {escape_html(str(exc))}", parse_mode="html")
            return

        folder = await self._pikpak_folder_for(user_id)
        transfers = (
            "magnet links, URLs, share links and Telegram media"
            if self._config.http.usable
            else "magnet links, URLs and share links"
        )
        account = (
            "your own account"
            if own
            else f"the shared account ({escape_html(self._config.pikpak.username)})"
        )
        footer = (
            "\n\n<code>/pikpak logout</code> disconnects your account."
            if own
            else "\n\n<code>/pikpak login</code> connects your own account instead."
        )
        await event.reply(
            "<b>PikPak</b>\n"
            f"Account: {account}\n"
            f"Storage: {human_size(quota.used)} of {human_size(quota.limit)} used "
            f"({quota.fraction * 100:.0f}%)\n"
            f"Folder: <code>{escape_html(folder or self._config.pikpak.folder)}</code>\n"
            f"Supported transfers: {transfers}"
            f"{footer}",
            parse_mode="html",
        )

    async def on_claim(self, event) -> None:
        """Take ownership of a freshly deployed bot, using the code in its log.

        Deliberately not behind the access check: before a claim there are no
        admins, so requiring one would make the bot unclaimable.
        """
        if not await bootstrap.claim_available(self._db, self._config):
            await event.reply("This bot already has an admin.")
            return

        parts = (event.raw_text or "").split()
        if len(parts) < 2:
            await event.reply(UNCLAIMED_HELP, parse_mode="html", link_preview=False)
            return

        try:
            await bootstrap.claim_admin(
                self._db, self._config, parts[1], event.sender_id
            )
        except bootstrap.ClaimError as exc:
            log.info("failed claim attempt by %s: %s", event.sender_id, exc)
            await event.reply(f"❌ {escape_html(str(exc))}", parse_mode="html")
            return

        await event.reply(
            "✅ <b>You are now the admin.</b>\n\n"
            "Nothing else needs deploying. Send <code>/setup</code> to sign in "
            "a reading account and connect PikPak, both from here.",
            parse_mode="html",
        )

    async def on_cache(self, event) -> None:
        """Choose the upload cache channel without hunting for its id."""
        if not await self._authorized(event):
            return
        if not self._config.access.is_admin(event.sender_id):
            await event.reply("Only an admin can change the upload cache.")
            return

        parts = (event.raw_text or "").split()
        argument = parts[1].lower() if len(parts) >= 2 else ""

        if argument in ("off", "none", "clear"):
            await bootstrap.clear_cache_chat(self._db, self._config)
            await event.reply(
                "Upload cache disabled. Every request downloads again."
            )
            return

        # Sent inside the channel itself: no id to look up at all.
        if not event.is_private:
            await self._use_cache_chat(event, event.chat_id)
            return

        if argument:
            ids = parse_id_list(argument)
            if not ids:
                await event.reply(
                    "That does not look like a chat id. Ids look like "
                    "<code>-1001234567890</code>.",
                    parse_mode="html",
                )
                return
            await self._use_cache_chat(event, ids[0])
            return

        current = self._config.delivery.cache_chat_id
        state = (
            f"Currently using <code>{current}</code>."
            if current
            else "No upload cache is set, so every request downloads again."
        )
        await event.reply(
            f"<b>Upload cache</b>\n\n{state}\n\n"
            "To set one: create a private channel, add me as an "
            "<b>administrator</b>, then post <code>/cache</code> "
            "<b>in that channel</b>. I will pick up its id myself.\n\n"
            "<code>/cache off</code> disables it.",
            parse_mode="html",
        )

    async def _use_cache_chat(self, event, chat_id: int) -> None:
        """Verify the bot can really use a chat as a cache, then store it."""
        try:
            permissions = await self._bot.get_permissions(chat_id, "me")
        except Exception as exc:
            await event.reply(
                "I cannot see that chat. Add me to it as an administrator "
                f"first.\n\n<i>{escape_html(str(exc))}</i>",
                parse_mode="html",
            )
            return

        if not getattr(permissions, "is_admin", False):
            await event.reply(
                "I am in that chat but not an administrator, so I could not "
                "store uploads there. Promote me and try again."
            )
            return

        await bootstrap.set_cache_chat(self._db, self._config, chat_id)
        await event.reply(
            f"✅ Upload cache set to <code>{chat_id}</code>.\n\n"
            "A link requested twice is now re-sent from Telegram instead of "
            "being downloaded again. This survives restarts, no redeploy.",
            parse_mode="html",
        )

    async def on_setup(self, event) -> None:
        """Show the setup checklist, or start one of its conversations."""
        if not await self._authorized(event):
            return
        if self._wizard is None:  # pragma: no cover - always attached in practice
            await event.reply("Setup is not available in this build.")
            return

        parts = (event.raw_text or "").split()
        action = parts[1].lower() if len(parts) >= 2 else ""

        if action == "cancel":
            stopped = await self._wizard.cancel(event.sender_id)
            await event.reply("Setup cancelled." if stopped else "Nothing to cancel.")
            return

        if action in ("pikpak", "telegram", "tg"):
            # Signing an account in to the bot is an operator action: it
            # decides what the whole bot can read, or where files land.
            if not self._config.access.is_admin(event.sender_id):
                await event.reply("Only an admin can run setup.")
                return
            if action == "pikpak":
                await self._wizard.begin_pikpak(event)
            else:
                await self._wizard.begin_telegram(event)
            return

        text = await self._wizard.status_text(event.sender_id)
        if not self._config.access.is_admin(event.sender_id):
            await event.reply(text, parse_mode="html", link_preview=False)
            return
        await event.reply(
            text
            + "\n\n<code>/setup telegram</code> · <code>/setup pikpak</code> · "
            "<code>/setup cancel</code>",
            parse_mode="html",
            link_preview=False,
        )

    async def on_verify(self, event) -> None:
        """Report the bot's identity and configuration. Admins only."""
        if not await self._authorized(event):
            return
        if not self._config.access.is_admin(event.sender_id):
            await event.reply("Only an admin can run /verify.")
            return

        notice = await event.reply("Checking…")
        try:
            report = await run_live_checks(
                self._config,
                bot=self._bot,
                user=self._user_client,
                pikpak=self._pikpak,
                portal=self._portal,
                for_user_id=event.sender_id,
            )
        except Exception as exc:
            log.exception("/verify failed")
            await notice.edit(f"❌ Verification failed: {escape_html(str(exc))}",
                              parse_mode="html")
            return

        await notice.edit(report.render_html(), parse_mode="html", link_preview=False)

    # ------------------------------------------------------------- dispatching

    async def on_message(self, event) -> None:
        """Handle anything that is not a command: links, or attached media."""
        # A setup conversation owns the next message the admin sends, so it
        # gets first refusal. It declines commands, which then fall through to
        # their own handlers.
        if self._wizard is not None and self._wizard.active(event.sender_id):
            if await self._wizard.handle(event):
                return

        text = event.raw_text or ""
        if text.startswith("/"):
            return  # handled by a command handler, or simply unknown

        bundle = extract_links(text)
        has_media = has_downloadable_media(event.message)

        # In a group, only act on messages that actually carry something to
        # download. Anything else is somebody else's conversation.
        if not event.is_private and not bundle and not has_media:
            return

        if not await self._authorized(event, quiet=not (bundle or has_media)):
            return

        if not bundle and has_media:
            await self._submit_inbound(event)
            return

        if not bundle:
            if bundle.errors:
                await self._report_errors(event, bundle)
                return
            await event.reply(
                "Send me a Telegram message link, a magnet link, or media to "
                "download. /help lists everything I understand."
            )
            return

        await self._submit_bundle(event, bundle)

    async def _report_errors(self, event, bundle: LinkBundle) -> None:
        lines = "\n".join(f"• {escape_html(item)}" for item in bundle.errors[:5])
        await event.reply(
            f"I could not use those links:\n{lines}", parse_mode="html"
        )

    async def _submit_inbound(self, event) -> None:
        """Queue media that was sent straight to the bot."""
        mode = await self._mode_for(event.sender_id)
        note = ""
        if mode == "telegram":
            # Sending the file back to the person who just sent it is pointless.
            mode = "local"
            note = " (mode <b>telegram</b> makes no sense here, saving locally)"

        job_id = await self._db.record_job(event.sender_id, "<attached media>", mode)
        job = Job(
            id=job_id,
            user_id=event.sender_id,
            chat_id=event.chat_id,
            mode=mode,
            kind=JobKind.INBOUND,
            label="attached media",
            message=event.message,
            pikpak_folder=await self._pikpak_folder_for(event.sender_id),
            reply_to=event.message.id,
        )
        try:
            await self._queue.submit(job)
        except QueueFull as exc:
            await self._db.finish_job(job_id, "failed", error=str(exc))
            await event.reply(f"❌ {exc}")
            return
        await event.reply(f"Queued <code>#{job_id}</code>{note}.", parse_mode="html")

    async def _submit_bundle(self, event, bundle: LinkBundle) -> None:
        """Queue every actionable item found in one incoming message."""
        user_id = event.sender_id
        mode = await self._mode_for(user_id)
        folder = await self._pikpak_folder_for(user_id)
        queued: list[int] = []
        rejected: list[str] = []

        async def enqueue(job: Job) -> None:
            try:
                await self._queue.submit(job)
                queued.append(job.id)
            except QueueFull as exc:
                await self._db.finish_job(job.id, "failed", error=str(exc))
                rejected.append(str(exc))

        for ref in bundle.messages:
            if not self._has_user_client and ref.is_private:
                rejected.append(
                    f"{ref.describe()} needs a user session; none is configured"
                )
                continue
            job_id = await self._db.record_job(user_id, ref.raw or ref.describe(), mode)
            await enqueue(
                Job(
                    id=job_id,
                    user_id=user_id,
                    chat_id=event.chat_id,
                    mode=mode,
                    kind=JobKind.MESSAGE,
                    label=ref.describe(),
                    ref=ref,
                    pikpak_folder=folder,
                    reply_to=event.message.id,
                )
            )

        # Magnet links and plain URLs can only go to PikPak: there is no
        # Telegram message behind them to send back.
        for url in bundle.magnets + bundle.direct_urls:
            job_id = await self._db.record_job(user_id, url, "pikpak")
            await enqueue(
                Job(
                    id=job_id,
                    user_id=user_id,
                    chat_id=event.chat_id,
                    mode="pikpak",
                    kind=JobKind.URL,
                    label=truncate(url, 60),
                    url=url,
                    pikpak_folder=folder,
                    reply_to=event.message.id,
                )
            )

        for url in bundle.pikpak_shares:
            job_id = await self._db.record_job(user_id, url, "pikpak")
            await enqueue(
                Job(
                    id=job_id,
                    user_id=user_id,
                    chat_id=event.chat_id,
                    mode="pikpak",
                    kind=JobKind.SHARE,
                    label=truncate(url, 60),
                    url=url,
                    pikpak_folder=folder,
                    reply_to=event.message.id,
                )
            )

        pieces: list[str] = []
        if queued:
            ids = ", ".join(f"#{job_id}" for job_id in queued)
            pieces.append(f"Queued {len(queued)} job(s): <code>{ids}</code> → <b>{mode}</b>")
        for problem in bundle.errors[:5] + rejected[:5]:
            pieces.append(f"• {escape_html(problem)}")

        if pieces:
            await event.reply("\n".join(pieces), parse_mode="html", link_preview=False)
