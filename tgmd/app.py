"""Application wiring and lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from . import bootstrap, botconfig, i18n
from .clients import start_clients
from .config import Config, ConfigError, load_config, parse_direct_endpoints
from .db import Database
from .delivery import Delivery
from .direct import DirectRouteV2
from .downloader import Downloader
from .forwarder import Forwarder
from .handlers import BotHandlers
from .identity import describe_account
from .parallel import MediaRoute
from .pikpak import PikPakService
from .portal import PikPakLoginPortal
from .resolver import Resolver
from .setup import USER_SESSION_KEY, SetupWizard, stored_user_session
from .tasks import JobQueue
from .utils import escape_html
from .webserver import FileServer
from .wms import WmsInBot
from .wms_panel import WmsPanel

log = logging.getLogger(__name__)


class SessionError(i18n.Explained, RuntimeError):
    """A session from the setup wizard that cannot be put into service."""


def setup_logging(level: str) -> None:
    """Configure root logging, keeping Telethon's own chatter down."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


class Application:
    """Owns every long-lived component and shuts them down in order."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.download.db_path)
        self.file_server: FileServer | None = None
        self.portal: PikPakLoginPortal | None = None
        self.pikpak: PikPakService | None = None
        self.queue: JobQueue | None = None
        self.handlers: BotHandlers | None = None
        self.wizard: SetupWizard | None = None
        self.bot = None
        self.user = None
        self.route: MediaRoute | None = None
        self.direct: DirectRouteV2 | None = None
        self.wms: WmsInBot | None = None
        self.wms_panel: WmsPanel | None = None
        self.session_rejected: tuple[str, str] | None = None
        """(where the session came from, why Telegram refused it), at startup."""
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        config = self.config
        config.ensure_directories()

        await self.db.connect()
        secret = await self.db.get_or_create_secret()

        # An admin claimed, or a cache channel chosen, on a previous run is
        # merged into the live config before anything reads it.
        await bootstrap.load_runtime_settings(self.db, config)

        # PikPak and its login portal come first: the portal registers its
        # routes on the same HTTP server that serves files to PikPak.
        self.pikpak = PikPakService(config.pikpak, self.db)
        self.portal = PikPakLoginPortal(
            self.pikpak,
            config.pikpak,
            config.http,
            bot_token=config.telegram.bot_token,
            # Bound, not copied: a later /claim appends to the same list.
            is_allowed=config.access.is_allowed,
        )
        # The warehouse runs on the same PikPak service; its admin panel is
        # served next to the login Mini App (WMS_ENABLED, docs/wms/).
        self.wms = WmsInBot(config, self.pikpak)
        self.wms_panel = WmsPanel(config, self.wms)
        self.file_server = FileServer(
            config.http, secret, portal=self.portal, routes=[self.wms_panel]
        )
        await self.file_server.start()

        # A session added by a previous in-chat login is picked up here, so
        # the wizard's work survives a restart.
        self.bot, self.user = await start_clients(
            config, await stored_user_session(self.db), on_rejected=self._session_rejected
        )
        # The user client reads history; without one the bot can only read
        # chats it belongs to itself, which still covers some setups.
        reading_client = self.user or self.bot

        delivery = Delivery(self.bot, config, self.db, self.pikpak, self.file_server)
        # TG_DIRECT_MEDIA=auto is refused (docs/wms/M7 §7.1): the direct media
        # connections reuse the main session's auth key, and seen from two IP
        # addresses at once (direct vs. proxy) Telegram answered with
        # AuthKeyDuplicatedError and revoked the reading account's session.
        if config.telegram.direct_media == "auto":
            log.error(
                "TG_DIRECT_MEDIA=auto is refused and treated as off: the direct media "
                "route reuses the session's auth key from a second IP address, which got "
                "the reading session revoked (AuthKeyDuplicatedError). See docs/HANDOFF.md."
            )
            config.telegram.direct_media = "off"
        self.route = None
        # TG_DIRECT_MEDIA=v2 (docs/wms/M7.1 §B): keys of its own, stored in
        # the database, for DCs other than the reading account's home DC.
        self.direct = (
            DirectRouteV2(
                self.db, manual=parse_direct_endpoints(config.telegram.direct_endpoints)[0]
            )
            if config.telegram.direct_media == "v2"
            else None
        )

        self.queue = JobQueue(
            config=config,
            db=self.db,
            bot=self.bot,
            resolver=Resolver(
                reading_client, auto_join=config.download.auto_join_invites
            ),
            downloader=Downloader(
                reading_client,
                connections=config.download.connections,
                route=self.route,
                direct=self.direct,
            ),
            bot_downloader=Downloader(
                self.bot,
                connections=config.download.connections,
                route=self.route,
                direct=self.direct,
            ),
            delivery=delivery,
            pikpak=self.pikpak,
            # Follows self.user, which /setup telegram can replace at runtime.
            forwarder=Forwarder(self.bot, config, self.db, reader=lambda: self.user),
            # Files that landed in PikPak get shelved by the WMS rules.
            after_pikpak=self.wms.pikpak_saved,
        )
        await self.queue.start()

        self.handlers = BotHandlers(
            self.bot,
            config,
            self.db,
            self.queue,
            self.pikpak,
            self.portal,
            user_client=self.user,
        )
        self.wizard = SetupWizard(
            self.bot,
            config,
            self.db,
            self.pikpak,
            self.portal,
            adopt_session=self.adopt_user_session,
            has_user_client=lambda: self.user is not None,
        )
        self.handlers.attach_wizard(self.wizard)
        self.handlers.attach_wms(self.wms, self.wms_panel)
        self.handlers.register()

        self.wms.attach_notifier(self._send_html)
        await self.wms.start()

        # Write the command menu and profile text ourselves, so nobody has to
        # paste them into @BotFather.
        await botconfig.apply(self.bot)

        # With no admin the bot refuses everyone, so tell the operator how to
        # claim it right where they are already looking: the deploy log.
        me = await self.bot.get_me()
        await bootstrap.announce_claim(
            self.db, config, getattr(me, "username", None)
        )

        log.info(
            "ready — mode %s, %d worker(s), %d connection(s) per file, direct media %s, "
            "cache chat %s, shared PikPak %s, PikPak Mini App %s",
            config.delivery.default_mode,
            config.download.concurrent,
            config.download.connections,
            config.telegram.direct_media,
            config.delivery.cache_chat_id or "disabled",
            "on" if config.pikpak.configured else "off",
            "on" if self.portal.unavailable_reason() is None else "off",
        )
        await self._tell_admins_session_rejected()

    async def _session_rejected(self, source: str, reason: str) -> None:
        """Telegram refused the reading account's session at startup (M7 §7.2).

        A dead session from an in-chat login is dropped from the database, so
        the next start does not try it again and /setup telegram starts clean.
        The admins are told once the bot can send messages.
        """
        if source == "an in-chat login":
            await self.db.kv_delete(USER_SESSION_KEY)
            log.warning("dropped the rejected in-chat user session from the database")
        self.session_rejected = (source, reason)

    async def _tell_admins_session_rejected(self) -> None:
        if self.session_rejected is None:
            return
        source, reason = self.session_rejected
        text = i18n.t("session.rejected", reason=escape_html(reason))
        if source == "TG_USER_SESSION":
            text += "\n" + i18n.t("session.rejected_env")
        for admin in self.config.access.admin_user_ids:
            try:
                await self._send_html(admin, text)
            except Exception:  # noqa: BLE001 - an admin who never opened the chat cannot be messaged
                log.warning("could not tell admin %s about the rejected session", admin)

    async def _send_html(self, chat_id: int, text: str, buttons=None) -> None:
        assert self.bot is not None
        await self.bot.send_message(chat_id, text, parse_mode="html", buttons=buttons,
                                    link_preview=False)

    async def run(self) -> None:
        """Serve until the process is asked to stop."""
        assert self.bot is not None
        disconnected = asyncio.create_task(self.bot.run_until_disconnected())
        stopping = asyncio.create_task(self._stopping.wait())
        done, pending = await asyncio.wait(
            {disconnected, stopping}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            # Surface a client-side failure rather than exiting silently.
            if task is disconnected and not task.cancelled():
                task.result()

    async def adopt_user_session(self, session_string: str) -> str:
        """Bring a session created by an in-chat login into service at once.

        Without this the wizard could only tell the operator to restart, which
        defeats the point of finishing setup inside Telegram. Jobs already
        running keep the client they started with; everything after this uses
        the new one.
        """
        try:
            session = StringSession(session_string)
        except ValueError as exc:
            # Telethon's own message here is just "Not a valid string".
            raise SessionError(key="err.session.not_a_session", error=exc) from exc

        client = TelegramClient(
            session,
            self.config.telegram.api_id,
            self.config.telegram.api_hash,
            flood_sleep_threshold=60,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise SessionError(key="err.session.not_authorized")

        account = await client.get_me()
        previous, self.user = self.user, client

        if self.queue is not None:
            self.queue.rebind_reader(
                Resolver(
                    client, auto_join=self.config.download.auto_join_invites
                ),
                Downloader(
                    client,
                    connections=self.config.download.connections,
                    route=self.route,
                    direct=self.direct,
                ),
            )
        if self.handlers is not None:
            self.handlers.set_user_client(client)

        if previous is not None:
            try:
                await previous.disconnect()
            except Exception:  # pragma: no cover - best effort
                log.debug("could not disconnect the previous user client", exc_info=True)

        label = describe_account(account)
        log.info("adopted a new reading account: %s", label)
        return label

    def request_stop(self) -> None:
        self._stopping.set()

    async def stop(self) -> None:
        log.info("shutting down")
        if self.wms is not None:
            await self.wms.stop()
        if self.queue is not None:
            await self.queue.stop()
        if self.file_server is not None:
            await self.file_server.stop()
        for client in (self.user, self.bot):
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # pragma: no cover - best effort
                    log.debug("client disconnect failed", exc_info=True)
        await self.db.close()


async def run_app(config_path: Path | None = None) -> None:
    """Load configuration, start everything, and run until stopped."""
    load_dotenv()
    config = load_config(config_path)
    setup_logging(config.log_level)
    # Before anything can produce a user-facing string.
    i18n.set_language(config.language)

    for warning in config.validate():
        log.warning(warning)

    app = Application(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows has no add_signal_handler; Ctrl-C still stops the loop there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, app.request_stop)

    await app.start()
    try:
        await app.run()
    finally:
        await app.stop()


def main(argv: list[str] | None = None) -> int:
    """Console entry point."""
    argv = sys.argv[1:] if argv is None else argv
    config_path = Path(argv[0]) if argv else None

    try:
        asyncio.run(run_app(config_path))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0
