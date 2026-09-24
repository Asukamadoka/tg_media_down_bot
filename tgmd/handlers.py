"""Bot commands and the link-dispatch handler."""

from __future__ import annotations

import logging

from telethon import TelegramClient, events

from . import bootstrap
from .buttons import webview_button
from .config import MODES, Config
from .db import Database
from .downloader import has_downloadable_media
from .i18n import display_mode, display_state, t
from .links import LinkBundle, extract_links
from .pikpak import PikPakError, PikPakService
from .portal import PikPakLoginPortal
from .tasks import Job, JobKind, JobQueue, QueueFull
from .utils import escape_html, human_size, parse_id_list, truncate
from .verify import run_live_checks

log = logging.getLogger(__name__)

# Every user-facing string now lives in :mod:`tgmd.i18n`.


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
            await event.reply(
                t("help.unclaimed"), parse_mode="html", link_preview=False
            )
            return False

        await event.reply(
            t("access.denied", user_id=user_id),
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
        text = t("help.body")
        if self._config.access.is_admin(event.sender_id):
            text += t("help.admin")
        if not self._has_user_client:
            text += t("help.no_reading_account")
            if self._config.access.is_admin(event.sender_id):
                text += t("help.no_reading_account_admin")
        await event.reply(text, parse_mode="html", link_preview=False)

    async def on_id(self, event) -> None:
        await event.reply(
            t("id.reply", user_id=event.sender_id, chat_id=event.chat_id),
            parse_mode="html",
        )

    async def on_mode(self, event) -> None:
        if not await self._authorized(event):
            return
        parts = (event.raw_text or "").split()
        current = await self._mode_for(event.sender_id)

        if len(parts) < 2:
            await event.reply(
                t("mode.current", current=display_mode(current)),
                parse_mode="html",
            )
            return

        choice = parts[1].lower()
        if choice not in MODES:
            await event.reply(
                t(
                    "mode.unknown",
                    choice=escape_html(choice),
                    modes=", ".join(MODES),
                ),
                parse_mode="html",
            )
            return
        if choice == "pikpak" and not await self._pikpak.available_for(event.sender_id):
            # /setup pikpak works on any deployment, so only the operator
            # switching user logins off leaves no way in.
            if self._pikpak.user_login_allowed:
                await event.reply(t("mode.pikpak_none"), parse_mode="html")
            else:
                await event.reply(t("mode.pikpak_unavailable"))
            return

        await self._db.set_user_mode(event.sender_id, choice)
        await event.reply(
            t("mode.set", choice=display_mode(choice)), parse_mode="html"
        )

    async def on_status(self, event) -> None:
        if not await self._authorized(event):
            return
        jobs = self._queue.snapshot(event.sender_id)
        if not jobs:
            await event.reply(t("status.empty"))
            return
        lines = [
            f"<code>#{job.id}</code> {display_state(job.state.value)} — "
            f"{escape_html(truncate(job.label, 60))}"
            for job in jobs
        ]
        await event.reply(
            t("status.header", count=len(jobs)) + "\n" + "\n".join(lines),
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
                t("cancel.no_match") if job_id else t("cancel.nothing")
            )
            return
        ids = ", ".join(f"#{job.id}" for job in cancelled)
        await event.reply(t("cancel.cancelling", ids=ids))

    async def on_stats(self, event) -> None:
        if not await self._authorized(event):
            return
        stats = await self._db.user_stats(event.sender_id)
        recent = await self._db.recent_jobs(event.sender_id, limit=8)

        if not stats and not recent:
            await event.reply(t("stats.empty"))
            return

        totals = " · ".join(
            f"{display_state(str(status))}: {values['count']}"
            for status, values in sorted(stats.items())
        )
        transferred = sum(values["bytes"] for values in stats.values())
        lines = [
            t(
                "stats.totals",
                totals=totals or t("stats.none"),
                transferred=human_size(transferred),
            )
        ]
        if recent:
            lines.append(t("stats.recent_header"))
            for job in recent:
                label = job.get("file_name") or job.get("link") or ""
                lines.append(
                    f"<code>#{job['id']}</code> "
                    f"{display_state(str(job['status']))} — "
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
        """Offer the Mini App where it can open, and the in-chat login otherwise."""
        if not self._pikpak.user_login_allowed:
            await event.reply(t("pikpak.login.disabled"))
            return
        # Telegram refuses web_app buttons outside a private chat, and a
        # password does not belong in a group either way.
        if not event.is_private:
            await event.reply(t("pikpak.login.private_only"))
            return

        already = await self._pikpak.has_user_session(event.sender_id)
        replacing = t("pikpak.login.replacing") if already else ""

        # Telegram signs the visitor's identity for a Mini App, so there is
        # no link or secret in a URL at all.
        miniapp = self._portal.miniapp_url
        if miniapp is not None:
            await event.reply(
                t("pikpak.login.miniapp", replacing=replacing),
                parse_mode="html",
                buttons=webview_button(t("pikpak.login.button_miniapp"), miniapp),
                link_preview=False,
            )
            return

        # No HTTPS address: the in-chat conversation needs none at all.
        await event.reply(
            t(
                "pikpak.login.chat_fallback",
                reason=escape_html(self._portal.unavailable_reason() or ""),
                replacing=replacing,
            ),
            parse_mode="html",
        )

    async def _pikpak_logout(self, event, parts: list[str]) -> None:
        """Clear the caller's own session, or the shared one for an admin."""
        scope = parts[2].strip().lower() if len(parts) >= 3 else ""
        if scope == "shared":
            if not self._config.access.is_admin(event.sender_id):
                await event.reply(t("pikpak.logout.only_admin_shared"))
                return
            await self._pikpak.logout()
            await event.reply(t("pikpak.logout.shared_cleared"))
            return

        if not await self._pikpak.has_user_session(event.sender_id):
            await event.reply(t("pikpak.logout.none"))
            return
        await self._pikpak.logout(event.sender_id)
        await event.reply(t("pikpak.logout.done"))

    async def _pikpak_dir(self, event, parts: list[str]) -> None:
        if len(parts) < 3:
            current = await self._pikpak_folder_for(event.sender_id)
            await event.reply(
                t(
                    "pikpak.dir.current",
                    folder=escape_html(current or self._config.pikpak.folder),
                ),
                parse_mode="html",
            )
            return
        folder = "/" + parts[2].strip().strip("/")
        await self._db.set_user_pikpak_dir(event.sender_id, folder)
        await event.reply(
            t("pikpak.dir.set", folder=escape_html(folder)),
            parse_mode="html",
        )

    async def _pikpak_status(self, event) -> None:
        user_id = event.sender_id
        own = await self._pikpak.has_user_session(user_id)

        if not await self._pikpak.available_for(user_id):
            if self._pikpak.user_login_allowed:
                await event.reply(t("pikpak.status.none"), parse_mode="html")
            else:
                await event.reply(t("mode.pikpak_unavailable"))
            return

        try:
            quota = await self._pikpak.quota(user_id=user_id)
        except PikPakError as exc:
            await event.reply(
                t("error.generic", error=escape_html(str(exc))), parse_mode="html"
            )
            return

        folder = await self._pikpak_folder_for(user_id)
        transfers = t(
            "pikpak.status.transfers_full"
            if self._config.http.usable
            else "pikpak.status.transfers_limited"
        )
        account = (
            t("pikpak.status.account_own")
            if own
            else t(
                "pikpak.status.account_shared",
                username=escape_html(self._config.pikpak.username),
            )
        )
        footer = t(
            "pikpak.status.footer_own" if own else "pikpak.status.footer_shared"
        )
        await event.reply(
            t(
                "pikpak.status.body",
                account=account,
                used=human_size(quota.used),
                limit=human_size(quota.limit),
                percent=f"{quota.fraction * 100:.0f}",
                folder=escape_html(folder or self._config.pikpak.folder),
                transfers=transfers,
                footer=footer,
            ),
            parse_mode="html",
        )

    async def on_claim(self, event) -> None:
        """Take ownership of a freshly deployed bot, using the code in its log.

        Deliberately not behind the access check: before a claim there are no
        admins, so requiring one would make the bot unclaimable.
        """
        if not await bootstrap.claim_available(self._db, self._config):
            await event.reply(t("claim.already"))
            return

        parts = (event.raw_text or "").split()
        if len(parts) < 2:
            await event.reply(
                t("help.unclaimed"), parse_mode="html", link_preview=False
            )
            return

        try:
            await bootstrap.claim_admin(
                self._db, self._config, parts[1], event.sender_id
            )
        except bootstrap.ClaimError as exc:
            log.info("failed claim attempt by %s: %s", event.sender_id, exc)
            await event.reply(
                t("error.generic", error=escape_html(str(exc))), parse_mode="html"
            )
            return

        await event.reply(t("claim.success"), parse_mode="html")

    async def on_cache(self, event) -> None:
        """Choose the upload cache channel without hunting for its id."""
        if not await self._authorized(event):
            return
        if not self._config.access.is_admin(event.sender_id):
            await event.reply(t("cache.only_admin"))
            return

        parts = (event.raw_text or "").split()
        argument = parts[1].lower() if len(parts) >= 2 else ""

        if argument in ("off", "none", "clear"):
            await bootstrap.clear_cache_chat(self._db, self._config)
            await event.reply(t("cache.disabled"))
            return

        # Sent inside the channel itself: no id to look up at all.
        if not event.is_private:
            await self._use_cache_chat(event, event.chat_id)
            return

        if argument:
            ids = parse_id_list(argument)
            if not ids:
                await event.reply(t("cache.bad_id"), parse_mode="html")
                return
            await self._use_cache_chat(event, ids[0])
            return

        current = self._config.delivery.cache_chat_id
        state = (
            t("cache.state_current", chat_id=current)
            if current
            else t("cache.state_none")
        )
        await event.reply(t("cache.help", state=state), parse_mode="html")

    async def _use_cache_chat(self, event, chat_id: int) -> None:
        """Verify the bot can really use a chat as a cache, then store it."""
        try:
            permissions = await self._bot.get_permissions(chat_id, "me")
        except Exception as exc:  # noqa: BLE001 - relay Telegram's reason, whatever it is
            await event.reply(
                t("cache.cannot_see", error=escape_html(str(exc))),
                parse_mode="html",
            )
            return

        if not getattr(permissions, "is_admin", False):
            await event.reply(
                t("cache.not_admin")
            )
            return

        await bootstrap.set_cache_chat(self._db, self._config, chat_id)
        await event.reply(
            t("cache.set", chat_id=chat_id),
            parse_mode="html",
        )

    async def on_setup(self, event) -> None:
        """Show the setup checklist, or start one of its conversations."""
        if not await self._authorized(event):
            return
        if self._wizard is None:  # pragma: no cover - always attached in practice
            await event.reply(t("setup.unavailable"))
            return

        parts = (event.raw_text or "").split()
        action = parts[1].lower() if len(parts) >= 2 else ""

        if action == "cancel":
            stopped = await self._wizard.cancel(event.sender_id)
            await event.reply(
                t("setup.cancelled") if stopped else t("setup.nothing_to_cancel")
            )
            return

        if action == "pikpak":
            # Connects the sender's own drive, exactly as the Mini App does,
            # so any allowed user may.
            await self._wizard.begin_pikpak(event)
            return
        if action in ("telegram", "tg"):
            # The reading account decides what the whole bot can read, so
            # signing one in is an operator action.
            if not self._config.access.is_admin(event.sender_id):
                await event.reply(t("setup.only_admin"))
                return
            await self._wizard.begin_telegram(event)
            return

        text = await self._wizard.status_text(event.sender_id)
        if not self._config.access.is_admin(event.sender_id):
            await event.reply(text, parse_mode="html", link_preview=False)
            return
        await event.reply(
            text + t("setup.footer"),
            parse_mode="html",
            link_preview=False,
        )

    async def on_verify(self, event) -> None:
        """Report the bot's identity and configuration. Admins only."""
        if not await self._authorized(event):
            return
        if not self._config.access.is_admin(event.sender_id):
            await event.reply(t("verify.only_admin"))
            return

        notice = await event.reply(t("verify.checking"))
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
            await notice.edit(
                t("verify.failed", error=escape_html(str(exc))), parse_mode="html"
            )
            return

        await notice.edit(report.render_html(), parse_mode="html", link_preview=False)

    # ------------------------------------------------------------- dispatching

    async def on_message(self, event) -> None:
        """Handle anything that is not a command: links, or attached media."""
        text = event.raw_text or ""
        if text.startswith("/"):
            # Telethon runs every matching handler, so this one also sees the
            # command that a command handler is already dealing with. A
            # command aborts an open setup conversation, so nobody is ever
            # trapped in the wizard — but /setup must not cancel the
            # conversation it has just opened, which is what made every
            # /setup telegram die before the first answer arrived.
            if self._wizard is not None and not text.startswith("/setup"):
                await self._wizard.cancel(event.sender_id)
            return  # handled by a command handler, or simply unknown

        # A setup conversation owns the next message the admin sends, so it
        # gets first refusal. handle() is only awaited when one is active.
        if (
            self._wizard is not None
            and self._wizard.active(event.sender_id)
            and await self._wizard.handle(event)
        ):
            return

        bundle = extract_links(text)
        has_media = has_downloadable_media(event.message)

        # In a group, only act on messages that actually carry something to
        # download. Anything else is somebody else's conversation.
        if not event.is_private and not bundle.actionable and not has_media:
            return

        if not await self._authorized(event, quiet=not (bundle.actionable or has_media)):
            return

        # In priority order; each branch looks at exactly one thing.
        if bundle.actionable:
            await self._submit_bundle(event, bundle)
        elif has_media:
            await self._submit_inbound(event)
        elif bundle.errors:
            await self._report_errors(event, bundle)
        else:
            await event.reply(t("dispatch.prompt"))

    async def _report_errors(self, event, bundle: LinkBundle) -> None:
        lines = "\n".join(f"• {escape_html(item)}" for item in bundle.errors[:5])
        await event.reply(
            t("dispatch.errors_header") + "\n" + lines, parse_mode="html"
        )

    async def _submit_inbound(self, event) -> None:
        """Queue media that was sent straight to the bot."""
        mode = await self._mode_for(event.sender_id)
        note = ""
        if mode in ("telegram", "auto"):
            # Sending the file back to the person who just sent it is pointless.
            mode = "local"
            note = t("inbound.note_local")

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
            await event.reply(
                t("error.generic", error=escape_html(str(exc))), parse_mode="html"
            )
            return
        await event.reply(
            t("inbound.queued", job_id=job_id, note=note), parse_mode="html"
        )

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
                    t("bundle.needs_user_session", ref=ref.describe())
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
            pieces.append(
                t(
                    "bundle.queued",
                    count=len(queued),
                    ids=ids,
                    mode=display_mode(mode),
                )
            )
        for problem in bundle.errors[:5] + rejected[:5]:
            pieces.append(f"• {escape_html(problem)}")

        if pieces:
            await event.reply("\n".join(pieces), parse_mode="html", link_preview=False)
